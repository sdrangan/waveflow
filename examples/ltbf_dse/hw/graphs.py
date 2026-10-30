"""graphs.py -- the job graphs the host's scheduler runs (spec §5.5; the plan's §1.4).

``plans/ltbf_dse/ltbf_dse.md`` step 4.4.  A :class:`Graph` is a list of :class:`Job` in a topological
order (each job's ``deps`` come before it), and the Shared Mem buffers its jobs read and write, in bus
words.  A buffer is written once (a new name a version: ``X3@2`` is CG job 3's ``X`` after iteration
2), so the memory plan (:func:`~examples.ltbf_dse.hw.sched.plan_memory`) can reuse its space once
every user is done.  Buffers the graph does not own are ``external``: the ring of ``Y`` chunks (the
Ingest DMA's) and ``G'_stack`` (the long-term graph's product, the real-time graph's input).

* :func:`long_term` -- one sector, one ``T_LT``: the SRS estimate (Python), explicit ``K`` once, per CG
  job of ``c`` columns ``t`` iterations of the systolic product(s) and the vector unit's CG step, then
  ``W = Ĥα'ᴴX_b`` and a CPU job a UE (Jacobi, ``G``, ``R̃``, Cholesky, ``G'``).  A long-term product is
  split into jobs of ``B_LT`` rows (the knob that bounds how long a real-time job waits).  CG jobs
  start at most ``cg_window`` ahead of the oldest unfinished one (its ``R₀ = D₀`` copy waits), which
  bounds their state in Shared Mem.
* :func:`real_time` -- one slot: symbol by symbol, chunk by chunk along frequency, the Ingest DMA's
  chunk (a root, completed by its ``Resp``), ``Z = G'_stack Y`` on the systolic array, the DMRS
  estimate (Python, one a slot: the estimator is full-band) from symbols 2-3's ``Z``, and on the
  vector unit ``x̂ = Σ conj(w) ⊙ z`` per data
  chunk; the measurement reads ``x̂``.  Symbols 0-1's ``Z`` wait in Shared Mem for ``w``.  A symbol's
  ``Z`` at a chunk position waits for the last use of the one ``rt_window`` symbols earlier, and a
  data symbol's ``x̂`` there for the measurement ``xh_window`` data symbols earlier: nothing else
  orders two symbols, so without them no plan safe under every order could reuse their space.

Engines: ``sys`` and ``vec`` (Job FIFOs), ``cpu`` (the host's worker threads), ``py`` (the zero-time
Python stages, run by the host), ``ingest`` (roots only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from waveflow.linalg.lanes import nwords

#: Job classes: real time ahead of long term.
RT, LT = 0, 1
#: A slot (30 kHz) and the long-term period.
SLOT_S = 125e-6
T_LT_S = 10e-3


def _pad(x: int, m: int) -> int:
    return -(-x // m) * m


@dataclass(frozen=True)
class Dims:
    """One configuration's sizes (spec §5.3): the systolic maxima are ``Kmax = Nmax = c``."""

    n_rx: int
    #: The sector's own UEs (CG columns, CPU jobs, real-time streams).
    U: int
    #: The UEs in ``Ĥα`` (own and the other sectors' connected ones); default ``U``.
    U_conn: int | None = None
    c: int = 8
    Mmax: int = 8
    r: int = 1
    t: int = 3
    form: str = "factored"
    J: int = 9
    n_sc: int = 792
    lane_bits: int = 16
    word_bits: int = 64
    #: Rows a long-term product job (a multiple of ``Mmax``); ``None``: unsplit.
    B_LT: int | None = None
    cg_window: int = 2
    #: Symbols a chunk position whose ``Z`` may be live at once (> the last DMRS symbol: 0-1 wait).
    rt_window: int = 4
    #: Data symbols a chunk position whose ``x̂`` may be live at once (the measurements free them).
    xh_window: int = 3
    n_sym: int = 14
    dmrs: tuple = (2, 3)

    def __post_init__(self) -> None:
        if self.form not in ("factored", "explicit"):
            raise ValueError(f"form {self.form!r}")
        if self.c % 8 or self.Mmax % 8:
            raise ValueError("c and Mmax are multiples of 8 (a UE's 8 columns)")
        if self.rt_window <= max(self.dmrs):
            raise ValueError(
                f"rt_window {self.rt_window} must exceed the DMRS symbols {self.dmrs}"
            )
        if self.xh_window < 1:
            raise ValueError(f"xh_window {self.xh_window} must be at least 1")
        if self.B_LT is not None and self.B_LT % self.Mmax:
            raise ValueError(f"B_LT {self.B_LT} is not a multiple of Mmax {self.Mmax}")

    @property
    def cols(self) -> int:
        """``Ĥα``'s ``8·U_conn`` columns padded to a multiple of ``Mmax`` and ``c``."""
        return _pad(8 * (self.U_conn or self.U), math.lcm(self.Mmax, self.c))

    @property
    def rows(self) -> int:
        """``N_rx`` padded to ``Kmax = c``."""
        return _pad(self.n_rx, self.c)

    @property
    def u_pad(self) -> int:
        """The own UEs padded to 8 (``U_pad``): a stream's rows in ``Z``, ``w`` and ``x̂``."""
        return _pad(self.U, 8)

    @property
    def S(self) -> int:
        """Real-time streams ``r·U_pad``, padded to ``Mmax``."""
        return _pad(self.r * self.u_pad, self.Mmax)

    @property
    def n_cg(self) -> int:
        """CG jobs: the own UEs' ``8U`` columns, ``c`` a job."""
        return -(-8 * self.U // self.c)

    @property
    def n_chunks(self) -> int:
        return -(-self.n_sc // (self.J * self.c))

    def words(self, rows: int, cols: int) -> int:
        return nwords(rows * cols, self.lane_bits, self.word_bits)


@dataclass
class Job:
    """A node: which engine runs it, its operation and shape, its buffers and dependencies."""

    jid: int
    engine: str
    op: str
    cls: int
    deps: tuple = ()
    reads: tuple = ()
    writes: tuple = ()
    #: ``M, K, N`` (and ``row0``) for a product; ``k, n, last`` for a CG step; ...
    shape: dict = field(default_factory=dict)
    deadline: float = math.inf
    #: The engine configuration it needs (a Ctrl write when it differs from the engine's).
    cfg: object = None
    #: What it is: ``("step", q, it)``, ``("chunk", sym, c)``, ...
    key: tuple = ()
    #: Order-only predecessors (a window's edge: no data flows, a drop does not follow it).
    after: tuple = ()


@dataclass
class Graph:
    """Jobs in a topological order, and the buffers (bus words) they use."""

    name: str
    cls: int
    t0: float = 0.0
    jobs: list = field(default_factory=list)
    buffers: dict = field(default_factory=dict)
    external: set = field(default_factory=set)
    #: The Ingest DMA's chunks in arrival order (real time): their root jobs' ids.
    chunks: list = field(default_factory=list)

    def add(self, engine, op, *, deps=(), reads=(), writes=(), shape=None, key=(), cfg=None,
            deadline=None, after=()) -> int:  # fmt: skip
        jid = len(self.jobs)
        for d in (*deps, *after):
            if not 0 <= d < jid:
                raise ValueError(
                    f"{self.name}: job {jid} depends on {d}, not an earlier job"
                )
        self.jobs.append(Job(jid, engine, op, self.cls, tuple(deps), tuple(reads), tuple(writes),
                             dict(shape or {}), math.inf if deadline is None else deadline, cfg,
                             tuple(key), tuple(after)))  # fmt: skip
        return jid

    def buffer(self, name: str, words: int, *, external: bool = False) -> str:
        self.buffers[name] = int(words)
        if external:
            self.external.add(name)
        return name

    def successors(self, *, data_only: bool = False) -> list[list[int]]:
        """Each job's successors: through every edge, or (*data_only*) through ``deps`` alone."""
        succ: list[list[int]] = [[] for _ in self.jobs]
        for j in self.jobs:
            for d in j.deps if data_only else (*j.deps, *j.after):
                succ[d].append(j.jid)
        return succ


def _product(g: Graph, d: Dims, op: str, a: str, b: str, out: str, M: int, K: int, N: int,
             deps, key, deadline) -> list[int]:  # fmt: skip
    """A systolic product ``out = A·B`` (``op`` MUL or MUL_AH), split into ``B_LT``-row jobs."""
    step = M if d.B_LT is None else d.B_LT
    return [
        g.add("sys", op, deps=deps, reads=(a, b), writes=(out,), deadline=deadline,
              shape={"M": min(step, M - r0), "K": K, "N": N, "row0": r0}, key=(*key, r0))
        for r0 in range(0, M, step)
    ]  # fmt: skip


def long_term(
    d: Dims, *, sector: int = 0, t0: float = 0.0, period: float = T_LT_S
) -> Graph:
    """One sector's long-term graph for one ``T_LT`` (spec §5.5); deadline the period's end."""
    g = Graph(f"lt{sector}", LT, t0)
    dl = t0 + period
    R, C, c = d.rows, d.cols, d.c
    hah = g.buffer("Hah", d.words(R, C))  # Ĥα, the MUL_AH A copy
    ham = g.buffer("Ham", d.words(R, C))  # Ĥα, the MUL A copy
    gp = g.buffer(
        "Gp", d.words(R, d.S), external=True
    )  # G'_stackᴴ, the real-time graph's input
    writes = [hah, ham]
    if d.form == "explicit":
        writes.append(g.buffer("HaHB", d.words(C, R)))
    srs = g.add("py", "srs", writes=writes, key=("srs",), deadline=dl)
    k_jobs: list[int] = []
    if d.form == "explicit":
        kb = g.buffer("K", d.words(R, R))
        k_jobs = _product(g, d, "MUL", ham, "HaHB", kb, R, C, R, (srs,), ("K",), dl)
    w_jobs: list[list[int]] = []
    for q in range(d.n_cg):
        x, r_, dd = (g.buffer(f"{v}{q}@0", d.words(R, c)) for v in "XRD")
        prev = w_jobs[q - d.cg_window] if q >= d.cg_window else []
        init = g.add("py", "init", deps=(srs,), after=prev, writes=(x, r_, dd), key=("init", q),
                     deadline=dl)  # fmt: skip
        last = [init, *k_jobs]
        for it in range(1, d.t + 1):
            if d.form == "factored":
                tb = g.buffer(f"T{q}@{it}", d.words(C, c))
                tj = _product(
                    g, d, "MUL_AH", hah, dd, tb, C, R, c, last, ("T", q, it), dl
                )
                ub = g.buffer(f"U{q}@{it}", d.words(R, c))
                uj = _product(g, d, "MUL", ham, tb, ub, R, C, c, tj, ("U", q, it), dl)
            else:
                ub = g.buffer(f"U{q}@{it}", d.words(R, c))
                uj = _product(g, d, "MUL", "K", dd, ub, R, R, c, last, ("U", q, it), dl)
            nx, nr, nd = (g.buffer(f"{v}{q}@{it}", d.words(R, c)) for v in "XRD")
            outs = (nx, nr, nd)
            if it == d.t:
                outs += (g.buffer(f"XB{q}", d.words(R, c)),)
            step = g.add("vec", "step", deps=(*uj, *last[:1]), reads=(x, r_, dd, ub),
                         writes=outs, shape={"k": R, "n": c, "last": it == d.t},
                         key=("step", q, it), deadline=dl)  # fmt: skip
            x, r_, dd, last = nx, nr, nd, [step]
        wb = g.buffer(f"W{q}", d.words(C, c))
        wj = _product(g, d, "MUL_AH", hah, f"XB{q}", wb, C, R, c, last, ("W", q), dl)
        w_jobs.append(wj)
        for i in range(q * c // 8, min((q + 1) * c // 8, d.U)):
            g.add("cpu", "proj", deps=wj, reads=(wb, x), writes=(gp,), key=("proj", i),
                  shape={"n_rx": d.n_rx, "r": d.r, "U": d.U}, deadline=dl)  # fmt: skip
    return g


def real_time(
    d: Dims, *, slot: int = 0, t0: float = 0.0, latency: float = SLOT_S
) -> Graph:
    """One slot's real-time graph (spec §5.5): its chunks arrive symbol by symbol, chunk by chunk;
    every job's deadline is the slot's end plus ``latency``."""
    g = Graph(f"rt{slot}", RT, t0)
    dl = t0 + SLOT_S + latency
    R, S, n = d.rows, d.S, d.J * d.c
    g.buffer("Gp", d.words(R, S), external=True)
    zj: dict[tuple, int] = {}
    last: dict[tuple, int] = {}  # the last job using symbol s's Z at chunk position c
    meas: dict[int, list] = (
        {}
    )  # chunk position -> its measurements, in data-symbol order
    for s in range(d.n_sym):
        for c in range(d.n_chunks):
            y = g.buffer(f"Y{s}:{c}", d.words(R, n), external=True)  # a ring slot
            ch = g.add("ingest", "chunk", writes=(y,), key=("chunk", s, c), deadline=dl)
            g.chunks.append(ch)
            z = g.buffer(f"Z{s}:{c}", d.words(S, n))
            win = (last[s - d.rt_window, c],) if s >= d.rt_window else ()
            zj[s, c] = g.add("sys", "MUL_AH", deps=(ch,), after=win, reads=("Gp", y), writes=(z,),
                             shape={"M": S, "K": R, "N": n, "row0": 0}, key=("Z", s, c),
                             deadline=dl)  # fmt: skip
        if s == max(d.dmrs):
            # One estimate for the slot: the DMRS estimator is full-band (spec §2: one phase slope
            # over the band, interpolation across it), and all of a symbol's chunks arrive at once.
            ws = tuple(g.buffer(f"w:{c}", d.words(S, n)) for c in range(d.n_chunks))
            wj = g.add("py", "dmrs", deps=tuple(zj[p, c] for p in d.dmrs for c in range(d.n_chunks)),
                       reads=tuple(f"Z{p}:{c}" for p in d.dmrs for c in range(d.n_chunks)),
                       writes=ws, key=("dmrs",), deadline=dl)  # fmt: skip
            for c in range(d.n_chunks):
                zj["w", c] = wj
                for p in d.dmrs:
                    last[p, c] = wj
        # The data symbols whose Z and w both exist by now (0-1 wait for the DMRS).
        if s >= max(d.dmrs):
            for p in [*range(min(d.dmrs)), s] if s == max(d.dmrs) else [s]:
                if p in d.dmrs:
                    continue
                for c in range(d.n_chunks):
                    xh = g.buffer(f"xh{p}:{c}", d.words(d.u_pad, n))
                    done = meas.setdefault(c, [])
                    win = (done[-d.xh_window],) if len(done) >= d.xh_window else ()
                    ej = g.add("vec", "eq", deps=(zj[p, c], zj["w", c]), after=win,
                               reads=(f"Z{p}:{c}", f"w:{c}"), writes=(xh,),
                               shape={"u_pad": d.u_pad, "c": d.c, "r": d.r, "tiles": d.J},
                               key=("eq", p, c), deadline=dl)  # fmt: skip
                    last[p, c] = g.add("py", "meas", deps=(ej,), reads=(xh,),
                                       key=("meas", p, c), deadline=dl)  # fmt: skip
                    done.append(last[p, c])
    return g
