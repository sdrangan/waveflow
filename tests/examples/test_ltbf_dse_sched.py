"""examples/ltbf_dse/hw/sched.py and graphs.py: the host's scheduler (plan step 4.4, AC10 unit tests).

The scheduler runs here on a small event bench (:class:`Bench`), not pysim: two engines with Job FIFOs
and a service time a job, the Resp FIFO (its occupancy watched, never bounded, so an overflow is seen
rather than hidden), a host that pops records one at a time with a latency and runs CPU jobs on its
cores, and an Ingest DMA that delivers a symbol's chunks at the symbol's end, or drops the symbol when
the ring has no room.  The bench records when every job starts and ends, every buffer's real lifetime,
and the Resp FIFO's peak.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import replace

import pytest

from examples.ltbf_dse.hw.graphs import LT, RT, Dims, Graph, long_term, real_time
from examples.ltbf_dse.hw.records import SRC_INGEST, SRC_SYS, SRC_VEC
from examples.ltbf_dse.hw.sched import (
    MemoryPlanError,
    Scheduler,
    footprint,
    plan_memory,
    release,
)

SRC = {"sys": SRC_SYS, "vec": SRC_VEC}
T_SYM = 125e-6 / 14


def service(job) -> float:
    """A job's time on its engine: a product by its MACs over a 64-MAC array, a CG step and an
    equalizer by their elements, a CPU job a flat 2 µs (250 MHz)."""
    s = job.shape
    if job.engine == "sys":
        return s["M"] * s["K"] * s["N"] / 64 / 250e6
    if job.engine == "vec":
        n = s["k"] * s["n"] if job.op == "step" else s["u_pad"] * s["c"] * s["tiles"]
        return (n / 4 + 50) / 250e6
    if job.engine == "cpu":
        return 2e-6
    return 0.0


class Bench:
    """The scheduler's world, as events (see the module docstring)."""

    def __init__(self, sch: Scheduler, *, qjob: int = 2, n_cores: int = 1, host_lat: float = 50e-9,
                 svc=service, n_chunks: int = 1, rt_start: float | None = None, n_sym: int = 0,
                 fail: tuple | None = None) -> None:  # fmt: skip
        self.s, self.qjob, self.n_cores, self.host_lat, self.svc = (
            sch,
            qjob,
            n_cores,
            host_lat,
            svc,
        )
        self.n_chunks, self.fail = n_chunks, fail
        self.ev: list = []
        self.k = 0
        self.now = 0.0
        self.fifo = {e: deque() for e in SRC}
        self.busy = {e: None for e in SRC}
        self.qresp: deque = deque()
        self.peak = 0
        self.host_busy = False
        self.cores = n_cores
        self.start: dict[int, float] = {}
        self.end: dict[int, float] = {}
        self.order = {e: [] for e in (*SRC, "py", "cpu")}
        # The Ingest DMA.
        self.taken = self.seq = 0
        self.dropped_symbols: list[int] = []
        if rt_start is not None:
            for sym in range(n_sym):
                self._at(rt_start + (sym + 1) * T_SYM, "symbol", sym)

    def _at(self, t: float, kind: str, arg=None) -> None:
        heapq.heappush(self.ev, (t, self.k, kind, arg))
        self.k += 1

    def run(self, until: float = 1.0) -> bool:
        """Run to the end; ``True`` when every job finished or was dropped (else a deadlock)."""
        self._dispatch()
        while self.ev and not self.s.finished:
            t, _k, kind, arg = heapq.heappop(self.ev)
            if t > until:
                break
            self.now = t
            getattr(self, f"_on_{kind}")(arg)
            self._dispatch()
        return self.s.finished

    # -- engines -----------------------------------------------------------------------------

    def _start(self, e: str) -> None:
        if self.busy[e] is None and self.fifo[e]:
            gid, tx = self.fifo[e].popleft()
            self.busy[e] = (gid, tx)
            self.start[gid] = self.now
            self._at(self.now + self.svc(self.s.jobs[gid]), "eng", e)

    def _on_eng(self, e: str) -> None:
        gid, tx = self.busy[e]
        self.busy[e] = None
        self.end[gid] = self.now
        status = (
            1
            if self.fail
            == (e, len([g for g in self.end if self.s.jobs[g].engine == e]))
            else 0
        )
        self._push((SRC[e], status, tx, 0))
        self._start(e)

    def _push(self, rec) -> None:
        self.qresp.append(rec)
        self.peak = max(self.peak, len(self.qresp))

    # -- the Ingest DMA ------------------------------------------------------------------------

    def _on_symbol(self, sym: int) -> None:
        free = self.s.ring_depth - (self.taken - self.s.credit)
        if free < self.n_chunks:
            self.dropped_symbols.append(sym)
        else:
            for c in range(self.n_chunks):
                self._push((SRC_INGEST, 0, 0, (self.seq + c) % (1 << 16)))
            self.taken += self.n_chunks
        self.seq += self.n_chunks

    # -- the host ------------------------------------------------------------------------------

    def _on_pop(self, _arg) -> None:
        self.host_busy = False
        src, status, tx, seq = self.qresp.popleft()
        gid = self.s.on_resp(src, status, tx, seq, self.now)
        if gid is not None and self.s.jobs[gid].engine == "ingest":
            self.start[gid] = self.end[gid] = self.now

    def _on_cpu(self, gid: int) -> None:
        self.cores += 1
        self.end[gid] = self.now
        self.s.host_done(gid, self.now)

    def _dispatch(self) -> None:
        s = self.s
        if s.error is not None:
            return
        while True:  # host jobs: Python stages at once, CPU jobs on free cores
            if self.cores == 0 and s._next("py") is None:
                break
            gid = s.take_host()
            if gid is None:
                break
            j = s.jobs[gid]
            self.order[j.engine].append(gid)
            self.start[gid] = self.now
            if j.engine == "py":
                self.end[gid] = self.now
                s.host_done(gid, self.now)
            else:
                self.cores -= 1
                self._at(self.now + self.svc(j), "cpu", gid)
            if self.cores == 0 and s._next("py") is None:
                break
        if not self.qresp and s.idle and not s.finished:
            # The host reads the DMA's status.
            s.on_status(self.taken, len(self.dropped_symbols), self.now)
        for e in SRC:
            for act in s.take(e, self.qjob - len(self.fifo[e])):
                if act[0] == "job":
                    self.fifo[e].append((act[1], act[2]))
                    self.order[e].append(act[1])
            self._start(e)
        if self.qresp and not self.host_busy:
            self.host_busy = True
            self._at(self.now + self.host_lat, "pop")

    # -- checks ---------------------------------------------------------------------------------

    def deps_respected(self) -> None:
        for gid, j in enumerate(self.s.jobs):
            if self.s.state[gid] != "done":
                continue
            base = gid - j.jid
            for d in (*j.deps, *j.after):
                if base + d in self.end:  # a dropped predecessor never ran
                    assert self.end[base + d] <= self.start[gid], (
                        j.key,
                        self.s.jobs[base + d].key,
                    )

    def lifetimes(self, g: Graph, base: int) -> dict[str, tuple[float, float]]:
        life: dict[str, list[float]] = {}
        for j in g.jobs:
            gid = base + j.jid
            if gid not in self.start:
                continue
            for b in j.writes:
                lo, hi = life.setdefault(b, [math.inf, -math.inf])
                life[b] = [min(lo, self.start[gid]), max(hi, self.end[gid])]
            for b in j.reads:
                lo, hi = life.setdefault(b, [math.inf, -math.inf])
                life[b] = [min(lo, self.start[gid]), max(hi, self.end[gid])]
        return {b: (lo, hi) for b, (lo, hi) in life.items() if b not in g.external}


SMALL = Dims(n_rx=16, U=4, c=8, Mmax=8, r=1, t=2, J=9, B_LT=8)


def system(policy="dynamic", d=SMALL, *, ring=None, qresp=64, **kw):
    """One long-term graph and one slot's real-time graph on one scheduler and bench."""
    ring = 2 * d.n_chunks if ring is None else ring
    s = Scheduler(
        policy=policy, qresp_depth=qresp, ring_depth=ring, n_chunks=d.n_chunks
    )
    lt, rt = long_term(d), real_time(d, t0=0.0)
    blt, brt = s.add(lt), s.add(rt)
    b = Bench(s, n_chunks=d.n_chunks, rt_start=0.0, n_sym=d.n_sym, **kw)
    return s, b, (lt, blt), (rt, brt)


@pytest.mark.parametrize("policy", ["dynamic", "static"])
@pytest.mark.parametrize("form", ["factored", "explicit"])
def test_every_dependency_is_respected_and_every_job_runs(policy, form):
    d = replace(SMALL, form=form)
    s, b, (lt, _), (rt, _) = system(policy, d)
    assert b.run()
    b.deps_respected()
    assert s.error is None and s.overruns == 0 and s.dropped == 0
    assert all(st == "done" for st in s.state)
    assert s.credit == d.n_sym * d.n_chunks  # every ring chunk returned
    n_sys = sum(j.engine == "sys" for j in [*lt.jobs, *rt.jobs])
    assert s.dispatched["sys"] == n_sys and len(s.in_flight) == 0


def test_graphs_have_the_spec_shape():
    d = SMALL
    lt = long_term(d)
    ops = [j.op for j in lt.jobs]
    # 8U = 32 columns, c = 8: 4 CG jobs, t = 2, B_LT = 8 rows: T (32 rows) 4 jobs, U (16 rows) 2.
    assert d.n_cg == 4 and ops.count("step") == 4 * 2 and ops.count("proj") == d.U
    assert sum(j.key[:1] == ("T",) for j in lt.jobs) == 4 * 2 * 4
    assert sum(j.key[:1] == ("U",) for j in lt.jobs) == 4 * 2 * 2
    assert sum(j.key[:1] == ("W",) for j in lt.jobs) == 4 * 4
    # CG job q's copy waits for job q - 2's W (cg_window 2).
    init2 = next(j for j in lt.jobs if j.key == ("init", 2))
    assert {lt.jobs[x].key[:2] for x in init2.after} == {("W", 0)}
    rt = real_time(d)
    assert len(rt.chunks) == d.n_sym * d.n_chunks == 14 * 11
    eq = [j for j in rt.jobs if j.op == "eq"]
    assert len(eq) == 12 * d.n_chunks and {j.key[1] for j in eq} == set(range(14)) - {
        2,
        3,
    }
    assert all(j.cls == RT for j in rt.jobs) and all(j.cls == LT for j in lt.jobs)
    # Symbol 4's Z at a chunk position waits for symbol 0's measurement there (rt_window 4).
    z4 = next(j for j in rt.jobs if j.key == ("Z", 4, 5))
    assert [rt.jobs[x].key for x in z4.deps] == [("chunk", 4, 5)]
    assert [rt.jobs[x].key for x in z4.after] == [("meas", 0, 5)]  # order only


def test_no_deadlock_at_the_smallest_fifo_and_ring_depths():
    """Job FIFOs of one job, a ring of one symbol, a Resp FIFO one record deeper than the ring, one
    CPU core: everything still finishes (the slot is spread so the ring can drain)."""
    d = replace(SMALL, n_sc=72, J=9)  # one chunk a symbol
    s = Scheduler(
        "dynamic",
        qresp_depth=d.n_chunks + 1,
        ring_depth=d.n_chunks,
        n_chunks=d.n_chunks,
    )
    s.add(long_term(d))
    s.add(real_time(d))
    b = Bench(s, qjob=1, n_cores=1, n_chunks=d.n_chunks, rt_start=0.0, n_sym=d.n_sym)
    assert b.run()
    b.deps_respected()
    assert b.peak <= s.qresp_depth and s.overruns == 0


@pytest.mark.parametrize("policy", ["dynamic", "static"])
def test_the_resp_fifo_never_overflows_behind_a_slow_host(policy):
    """A host 20x slower at popping than an engine is at finishing small jobs: the records waiting in
    the Resp FIFO never exceed its depth (jobs in flight + ring <= depth)."""
    d = replace(SMALL, B_LT=8)
    s, b, *_ = system(
        policy, d, ring=d.n_chunks, qresp=d.n_chunks + 4, qjob=4, host_lat=2e-6
    )
    assert b.run()
    assert b.peak <= s.qresp_depth
    assert s.max_in_flight == 4
    # The ring of one symbol overflows behind the slow host: every drop is counted, the last ones
    # from the DMA's status (no later record shows their gap).
    assert s.overruns == len(b.dropped_symbols)


def test_the_memory_plan_fits_and_holds_under_the_real_order():
    """Each graph's plan is within its capacity, and any two buffers whose real lifetimes overlapped
    in the run were placed apart."""
    d = replace(SMALL, form="explicit")
    _s, b, (lt, blt), (rt, brt) = system("dynamic", d)
    plans = {}
    for g, base in ((lt, blt), (rt, brt)):
        need = sum(g.buffers[x] for x in g.buffers if x not in g.external)
        plans[g.name] = (plan_memory(g, need), base, g)
        assert footprint(g, plans[g.name][0]) <= need
    assert b.run()
    for plan, base, g in plans.values():
        life = b.lifetimes(g, base)
        names = sorted(plan)
        for i, x in enumerate(names):
            for y in names[i + 1 :]:
                if (
                    x in life
                    and y in life
                    and life[x][0] < life[y][1]
                    and life[y][0] < life[x][1]
                ):
                    ax, ay = plan[x], plan[y]
                    assert ax + g.buffers[x] <= ay or ay + g.buffers[y] <= ax, (x, y)
    # The plans reuse space: far less than every buffer apart (real time by its rt_window).
    for g in (lt, rt):
        need = sum(g.buffers[x] for x in g.buffers if x not in g.external)
        assert footprint(g, plans[g.name][0]) < need / 2
    with pytest.raises(MemoryPlanError):
        plan_memory(lt, footprint(lt, plans["lt0"][0]) - 1)


def test_deadline_misses_and_ring_overruns_are_counted():
    """A constructed case: one chunk a symbol, a ring of one, ``Z`` takes 2.5 symbols.  Symbol 0 is
    taken; 1 and 2 arrive while its slot is busy and are dropped (2 overruns; 2 is a DMRS symbol, so
    every chunk's ``w`` and so every equalizer job goes with it); symbol 3 is taken once 0's slot is
    back.  With every deadline at 5.6 symbols, ``Z`` of symbol 0 (ends at 3.5) meets it and ``Z`` of
    symbol 3 (ends at 6.5) misses: 1 miss."""
    d = Dims(n_rx=16, U=4, c=8, Mmax=8, J=9, n_sc=72, n_sym=4)
    s = Scheduler("dynamic", qresp_depth=8, ring_depth=1, n_chunks=1)
    rt = real_time(d)
    for j in rt.jobs:
        j.deadline = 5.6 * T_SYM
    s.add(rt)

    def svc(job):
        return 2.5 * T_SYM if job.op == "MUL_AH" else 0.0

    b = Bench(s, qjob=1, svc=svc, host_lat=0.0, n_chunks=1, rt_start=0.0, n_sym=4)
    assert b.run()
    assert b.dropped_symbols == [1, 2] and s.overruns == 2
    assert s.misses == {RT: 1, LT: 0}
    z = {j.key: gid for gid, j in enumerate(s.jobs) if j.op == "MUL_AH"}
    assert s.state[z["Z", 0, 0]] == s.state[z["Z", 3, 0]] == "done"
    assert s.state[z["Z", 1, 0]] == s.state[z["Z", 2, 0]] == "dropped"
    assert all(
        s.state[g] == "dropped" for g, j in enumerate(s.jobs) if j.op in ("eq", "dmrs")
    )
    assert s.credit == 2  # symbols 0 and 3


def test_an_engine_error_stops_dispatch():
    s, b, *_ = system("dynamic", fail=("vec", 3))  # the third vector-unit job fails
    assert not b.run()
    gid, status = s.error
    assert status == 1 and s.jobs[gid].engine == "vec"
    n = sum(s.dispatched.values())
    assert (
        s.take("sys") == []
        and s.take_host() is None
        and sum(s.dispatched.values()) == n
    )


def test_the_ring_is_returned_in_order():
    """Three chunks arrive; their ``Z`` jobs finish 2, 0, 1: no credit until chunk 0's slot is free,
    then all three."""
    d = Dims(n_rx=16, U=4, c=8, Mmax=8, J=1, n_sc=24, n_sym=1, dmrs=(0,))
    s = Scheduler("dynamic", qresp_depth=8, ring_depth=3, n_chunks=3)
    s.add(real_time(d))
    for c in range(3):
        s.on_resp(SRC_INGEST, 0, 0, c, 0.0)
    jobs = {act[1]: act[2] for act in s.take("sys") if act[0] == "job"}
    by_c = {s.jobs[g].key[2]: (g, tx) for g, tx in jobs.items()}
    credits = []
    for c in (2, 0, 1):
        s.on_resp(SRC_SYS, 0, by_c[c][1], 0, 1e-6)
        credits.append(s.credit)
    assert credits == [0, 1, 3]
    assert [s.slot(g) for g in s._slot_of] == [0, 1, 2]


def test_ctrl_writes_only_when_the_configuration_changes():
    g = Graph("cfg", RT)
    prev = ()
    for cfg in ("a", "a", "b", "b", "a"):
        prev = (
            g.add("sys", "MUL", deps=prev, cfg=cfg, shape={"M": 8, "K": 8, "N": 8}),
        )
    s = Scheduler("dynamic")
    s.add(g)
    acts = []
    for _ in range(5):
        a = s.take("sys")
        acts += a
        s.on_resp(SRC_SYS, 0, a[-1][2], 0, 0.0)
    assert [a[1] for a in acts if a[0] == "ctrl"] == [
        "a",
        "b",
        "a",
    ] and s.ctrl_writes == 3
    assert sum(t["op"] == "ctrl" for t in s.trace) == 3


def test_static_order_is_the_graphs_whatever_the_timing():
    """The static policy dispatches each engine's jobs in the graphs' order, so two runs with
    different service times give the same per-engine sequences (the RTL gate relies on it) -- as
    long as no symbol is dropped (a dropped symbol's jobs never run).  The second run's vector unit
    is 1.6x slower and its systolic array 1.6x faster; it ends 61 µs later.
    """
    seqs = []
    for scale in (1.0, 1.6):
        s, b, *_ = system(
            "static",
            svc=lambda j, k=scale: service(j) * (k if j.engine == "vec" else 1 / k),
        )
        assert b.run() and s.overruns == 0
        seqs.append({e: list(b.order[e]) for e in ("sys", "vec", "cpu", "py")})
        for e in ("sys", "vec", "cpu", "py"):
            assert b.order[e] == sorted(b.order[e])
    assert seqs[0] == seqs[1]


def test_the_trace_prices_every_decision():
    """Each dynamic-policy job is added to and removed from a ready list once (the calibrated
    ``sched_ops`` operations on the real list), each engine job is a ``dispatch``, each finished job
    a ``release`` whose counters sum to the graph's edges."""
    s, b, (lt, _), (rt, _) = system("dynamic")
    assert b.run()
    ops = [t["op"] for t in s.trace]
    n_run = sum(j.engine != "ingest" for j in [*lt.jobs, *rt.jobs])
    assert ops.count("sched_ops.add") == ops.count("sched_ops.delete") == n_run
    assert ops.count("dispatch") == s.dispatched["sys"] + s.dispatched["vec"]
    assert ops.count("release") == len(s.jobs)
    edges = sum(len(j.deps) + len(j.after) for j in [*lt.jobs, *rt.jobs])
    assert sum(t["n_succ"] for t in s.trace if t["op"] == "release") == edges
    for t in s.trace:
        if t["op"] == "sched_ops.delete":
            assert t["n_scanned"] == 1 and t["n_moved"] == t["n_tasks"] - 1  # the head


def test_release_twin():
    pending = [0, 2, 1, 1]
    ready, cnt = release([1, 2, 3], pending)
    assert ready == [2, 3] and pending == [0, 1, 0, 0]
    assert cnt == {"n_succ": 3.0, "n_ready": 2.0}
