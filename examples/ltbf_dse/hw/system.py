"""system.py -- the LTBF system in pysim: a host running the scheduler, both engines, the Ingest DMA (spec §5).

``plans/ltbf_dse/ltbf_dse.md`` step 4.5 (§1.2's architecture).  One crossbar joins:

* the **host** (:class:`LtbfHost`, a ``SwHost``): its bus master, the Resp FIFO's interrupt, and an A53
  :class:`~waveflow.cpu.processor.Processor` its threads charge.  The dispatcher thread runs the
  scheduler (:mod:`~examples.ltbf_dse.hw.sched`) on the job graphs (:mod:`~examples.ltbf_dse.hw.graphs`):
  it writes engine jobs into the Job FIFOs, runs the Python stages (zero time; their results reach
  Shared Mem over its bus), hands CPU jobs to its worker threads, and returns ring credit.  A reader
  thread moves ``qresp`` records into the dispatcher's inbox;
* the **systolic wrapper** (``SysWrap``), the **vector unit's wrapper** (``VecWrap``, one build for the
  CG step and the equalizer), the **Ingest DMA** (``IngestDev``), the **RespHub** and **Shared Mem**;
* the **O-RU** stand-in (:class:`OruSource`): the front end's quantized grid ``Y = q_bc(2^-k y)``,
  antenna by antenna, a symbol every 125/14 µs once the slot is released.

The run (AC9's shape): one long-term update of one sector, then one slot.  Every Python stage and CPU
job uses the fixed-point chain's own code on what it reads from Shared Mem (``chest``,
``ltbf_fixed_chain.project_ue``), so Shared Mem ends equal to :func:`golden` bit for bit.  The host
records each of its jobs' bus writes and charged cycles (:attr:`LtbfHost.replay`): the file the RTL
gate's C++ host replays.

Shared Mem is at bus address 0 and the devices above ``0x8000_0000`` (the engines address memory in
words from 0); the layout: the programs, ``G'_stackᴴ``, the ring, then the two graphs' plans.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from examples.ltbf_dse import a53cal, ltbf_proj, sched_release
from examples.ltbf_dse import chest as ce
from examples.ltbf_dse import ltbf_chain as lc
from examples.ltbf_dse import ltbf_fixed as lf
from examples.ltbf_dse import ltbf_fixed_chain as fc
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.hw import programs as pg
from examples.ltbf_dse.hw.graphs import Dims, long_term, real_time
from examples.ltbf_dse.hw.ingest import IngestDev
from examples.ltbf_dse.hw.records import IngestCfg, Resp
from examples.ltbf_dse.hw.resp_hub import RespHub
from examples.ltbf_dse.hw.sched import Scheduler, footprint, plan_memory
from examples.ltbf_dse.hw.sys_wrap import SysWrap
from examples.ltbf_dse.hw.vec_wrap import VecMemJob, VecWrap
from waveflow.cpu.platform import CpuPlatform
from waveflow.cpu.processor import Processor
from waveflow.cpu.task import SwFunction
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.interface import StreamIF, StreamIFMaster
from waveflow.hw.irq import IrqIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, MemSlaveLayout
from waveflow.linalg.lanes import from_words, to_words
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine_mem import EngineJobDesc, block_major
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost, SwLock, SwQueue
from waveflow.vecunit.asm import assemble

F_HOST = 250e6
T_SYM = 125e-6 / 14
SYS_BASE, VEC_BASE, ING_BASE, HUB_BASE = (
    0x8000_0000,
    0x8001_0000,
    0x8002_0000,
    0x8003_0000,
)


@dataclass(frozen=True)
class SysParams:
    """One system run's configuration; the defaults are AC9's (spec §6 C: ``c`` 8, ``Mmax`` 16)."""

    drop: int = 1
    n: int = 8  # the array's side: N_rx = n²
    target: str = "-6_14"
    sector: int = 1
    W: int = 16  # the operand width of the fixed-point configuration
    t: int = 3
    r: int = 1
    c: int = 8
    Mmax: int = 16
    J: int = 9
    ring_symbols: int = 2  # the ring's depth, in symbols
    qresp_depth: int = 64
    policy: str = "static"
    B_LT: int | None = None
    n_cores: int = 1
    L: int = 8  # the vector unit's lanes
    word_bits: int = 64
    lane_bits: int = 16
    cg_window: int = 2
    rt_window: int = 4
    #: The O-RU's symbol period.  A real slot is 125/14 µs a symbol; one 64-bit port cannot carry
    #: 64 antennas at that rate (spike d), so a correctness run paces the slot slower.
    sym_period: float = T_SYM


# -- the inputs and the fixed-point chain's answer ------------------------------------------------------


@dataclass
class Inputs:
    """What the run needs from Python: the scenario, the formats, the stored operands, the answer."""

    p: SysParams
    prep: lc.Prepared
    cfg: fc.FixedConfig
    dims: Dims
    own: list  # the sector's own UEs, in its RHS's column order
    sh: int  # the sector's scale s
    h: tuple  # Ĥα' stored in cfg.a, (N_rx, 8·n_conn)
    b: tuple  # B' stored in bc, (N_rx, 8·U)
    y: tuple  # Y stored in bc, (N_rx, 14, 792)
    gold: dict = field(default_factory=dict)

    @property
    def back(self) -> float:
        return 2.0 ** (self.cfg.g_shift + self.cfg.k_shift)


def inputs(p: SysParams) -> Inputs:
    """Render and estimate (``ltbf_chain.prepare``), profile the formats on this drop, and compute
    the fixed-point chain's answer for the sector (``golden``)."""
    prep = lc.prepare(p.drop, p.n, lk.TARGETS[p.target])
    ranges = fc.profile(prep, t_max=p.t, ranks=(p.r,))
    cfg = fc.formats(ranges, p.W)
    s = p.sector
    own = [int(u) for u in prep.ltin.own[s]]
    ha = prep.ltin.h_alpha[s]
    n_conn = ha.shape[1] // lk.N_SRS
    sh = ce.sector_scale(ha, prep.log)
    h = lf.to_stored(ha * 2.0**-sh, cfg.a)
    b = lf.to_stored(prep.ltin.rhs[s] * 2.0 ** (cfg.q_shift - sh), cfg.bc)
    y = lf.to_stored(prep.y[s] * 2.0**-cfg.k_shift, cfg.bc)
    dims = Dims(n_rx=p.n * p.n, U=len(own), U_conn=n_conn, c=p.c, Mmax=p.Mmax, r=p.r, t=p.t,
                J=p.J, lane_bits=p.lane_bits, word_bits=p.word_bits, B_LT=p.B_LT,
                cg_window=p.cg_window, rt_window=p.rt_window)  # fmt: skip
    if dims.S != p.Mmax:
        raise ValueError(
            f"the streams r·U_pad = {dims.S} must fill one Mmax = {p.Mmax} row tile"
        )
    inp = Inputs(p, prep, cfg, dims, own, sh, h, b, y)
    inp.gold = golden(inp)
    return inp


def golden(inp: Inputs) -> dict:
    """The fixed-point chain on the sector (``ltbf_fixed_chain``, as ``run_fixed`` does it): ``G'``
    stored per UE, and per UE ``Z`` (stored), ``w`` (stored) and ``x̂`` (stored)."""
    p, cfg, prep = inp.p, inp.cfg, inp.prep
    lto = fc.fixed_long_term(prep, cfg, p.t)
    gp = fc.fixed_projection(prep, cfg, lto, p.t, p.r)
    yr, yi = (v.reshape(v.shape[0], -1) for v in inp.y)
    out: dict = {"g": {}, "z": {}, "w": {}, "xh": {}}
    for u in inp.own:
        gr, gi = lf.to_stored(gp[u].conj().T * 2.0**-cfg.g_shift, cfg.a)
        out["g"][u] = (gr, gi)
        zr, zi = lf.mm(gr, gi, cfg.a, yr, yi, cfg.bc, cfg.bc, adjoint=True)
        zr, zi = zr.reshape(p.r, sg.N_SYM, sg.N_SC), zi.reshape(p.r, sg.N_SYM, sg.N_SC)
        out["z"][u] = (zr, zi)
        z = lf.to_complex(zr, zi, cfg.bc) * inp.back
        _h, w = ce.dmrs_weights(z[:, list(sg.DMRS_SYMS)], int(prep.ports[u]), p.sector,
                                prep.log)  # fmt: skip
        wr, wi = lf.to_stored(w * inp.back, cfg.eq.w)
        out["w"][u] = (wr, wi)
        d = list(sg.DATA_SYMS)
        out["xh"][u] = fc.equalize_fixed(zr[:, d], zi[:, d], wr, wi, cfg.eq)
    return out


# -- the O-RU ------------------------------------------------------------------------------------------


@dataclass
class OruSource(FreeRunMod):
    """The O-RU stand-in: once ``start`` fires, a symbol every ``period``, antenna by antenna."""

    symbols: list = field(default_factory=list)
    period: float = T_SYM
    word_bits: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=F_HOST))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=self.word_bits
        )
        self.add_endpoint(self.m_out)
        self.start = self.env.event()

    def run_iter(self):
        yield self.start
        t0 = self.env.now
        for s, bursts in enumerate(self.symbols):
            yield self.env.timeout(max(0.0, t0 + (s + 1) * self.period - self.env.now))
            for b in bursts:
                yield from self.m_out.write(b)
        yield self.env.event()  # done: wait forever


# -- the host ------------------------------------------------------------------------------------------


def host_cycles(cpu_cycles: float, f_cpu: float, f_host: float = F_HOST) -> int:
    """The host-clock cycles a call of *cpu_cycles* A53 cycles is charged (``compute(n)``)."""
    return math.ceil(cpu_cycles * f_host / f_cpu - 1e-9)


def proj_cycles(n_rx: int, n_conn: int, r: int) -> float:
    """**Provisional** A53 cycles of one UE's CPU job when ``ltbf_proj`` has no local fit: its
    complex MACs (``8r·N_rx`` for ``G``, ``r²(N_rx + 8·n_conn)`` for ``R̃``, ``r²·N_rx`` for ``G'``)
    at 8 cycles each, and an 8x8 Jacobi at 30,000."""
    cmacs = 8 * r * n_rx + r * r * (n_rx + 8 * n_conn) + r * r * n_rx
    return 8.0 * cmacs + 30_000.0


class _ProvisionalProj:
    """:func:`proj_cycles` on the job's counters (no local calibration of ``ltbf_proj``)."""

    def predict_feat(self, feats) -> float:
        return proj_cycles(int(feats["n"]), int(feats["conn"]), int(feats["r"]))


@dataclass(frozen=True)
class _Linear:
    """A provisional cycle model, ``a + Σ b_f · f`` (a calibrated family replaces it in step 4.6)."""

    a: float
    b: dict

    def predict_feat(self, feats) -> float:
        return self.a + sum(v * float(feats.get(k, 0.0)) for k, v in self.b.items())


@dataclass
class LtbfHost(SwHost):
    """The host (see the module docstring)."""

    cpu: Processor | None = None
    platform: Any = None
    clk: Clock = field(default_factory=lambda: Clock(freq=F_HOST))
    cpp_model: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=64)
        self.irq = {
            v: self.add_irq(f"irq_{v}") for v in ("qjob_sys", "qjob_vec", "qresp")
        }
        self.done = self.env.event()
        self.sys: Any = None  # set by build(): the run's plan and endpoints
        self.replay: list[dict] = []
        self.results: dict = {"xh": {}, "z_dmrs": {}, "w": {}}
        self.resp_log: list[tuple] = []
        self.jobs_log: dict = {"sys": [], "vec": []}
        #: Which price each step 4.6 kernel got: "calibrated" (the local fit) or "provisional".
        self.pricing: dict[str, str] = {}
        model = a53cal.load_model(ltbf_proj.CAL)
        self.pricing["ltbf_proj"] = "calibrated" if model is not None else "provisional"
        self.proj_fn = SwFunction(
            name="ltbf_proj", fn=lambda **kw: (None, kw),
            cycles=model or _ProvisionalProj())  # fmt: skip

    def scenario_bursts(self):
        return []

    # -- charging -----------------------------------------------------------------------------

    def charge(self, func: SwFunction, ctx: dict | None = None, **kw):
        """Run *func* on the Processor, its core time padded to whole host cycles (spike e); the
        cycles go to the job context *ctx*, or (the scheduler's own) to :attr:`sched_cycles`.
        """
        result = yield from self.cpu.execute(func, **kw)
        rec = self.cpu.records[-1]
        n = host_cycles(
            rec.cycles + rec.switch_cycles, self.cpu.config.f_clk_hz, self.clk.freq
        )
        pad = rec.t_start + n * self.clk.period - self.env.now
        if pad > 0:
            yield self.env.timeout(pad)
        if ctx is not None:
            ctx["cycles"] += n
        else:
            self.sched_cycles.append(n)
        return result

    def _sched_fn(self, family: str) -> SwFunction:
        if (
            family == "release"
        ):  # step 4.6's kernel: its local fit, or a provisional price
            model = a53cal.load_model(sched_release.CAL)
            self.pricing["release"] = (
                "calibrated" if model is not None else "provisional"
            )
            return SwFunction(name="release", fn=lambda **kw: (None, kw),
                              cycles=model or _Linear(20.0, {"n_succ": 12.0}))  # fmt: skip
        if family == "ctrl":
            return SwFunction.fixed("ctrl", 40.0)
        return SwFunction(name=family, fn=lambda **kw: (None, kw),
                          cycles=self.platform.model(family, "cycles"),
                          energy_pj=self.platform.model(family, "energy_pj"))  # fmt: skip

    def _charge_trace(self):
        sch = self.sys.sch
        while self._traced < len(sch.trace):
            t = dict(sch.trace[self._traced])
            self._traced += 1
            op = t.pop("op")
            t.pop("engine", None)
            yield from self.charge(self._fns[op], **{k: float(v) for k, v in t.items()})

    # -- the bus --------------------------------------------------------------------------------

    def write_words(self, word_addr: int, words, ctx: dict | None = None) -> Any:
        words = np.asarray(words, np.uint64)
        if ctx is not None:
            ctx["writes"].append((int(word_addr), words.copy()))
        yield from self.m.write(words, word_addr * 8)

    def read_words(self, word_addr: int, n: int):
        return np.asarray((yield from self.m.read(int(n), word_addr * 8)), np.uint64)

    # -- threads --------------------------------------------------------------------------------

    def main(self):
        S = self.sys
        self._fns = {f: self._sched_fn(f) for f in ("sched_ops.add", "sched_ops.delete",
                                                     "dispatch", "release", "ctrl")}  # fmt: skip
        self._traced = 0
        self.sched_cycles: list[int] = []
        self.inbox = SwQueue(self, name="inbox")
        self.cpu_q = SwQueue(self, name="cpu_q")
        self.gp_lock = SwLock(self, name="gp")
        self.start(self._resp_reader, name="resp")
        for k in range(S.p.n_cores):
            self.start(self._cpu_worker, name=f"worker{k}")
        setup = {"key": ["setup"], "op": "programs", "writes": [], "cycles": 0}
        self.replay.append(setup)
        for a, words in S.programs.values():
            yield from self.write_words(a, words, setup)
        yield from S.ing_ctrl.write(S.ing_cfg)
        S.sch.add(S.lt)
        rt_added = False
        yield from self._pump()
        while not (rt_added and S.sch.finished):
            ev = yield from self.inbox.get()
            if ev[0] == "resp":
                r = ev[1]
                self.resp_log.append(
                    (int(r.src), int(r.tx_id), int(r.seq), self.env.now)
                )
                gid = S.sch.on_resp(
                    int(r.src), int(r.status), int(r.tx_id), int(r.seq), self.env.now
                )
                if S.sch.error is not None:
                    raise RuntimeError(f"engine error {S.sch.error}")
                if gid is not None and S.sch.jobs[gid].engine == "ingest":
                    S.chunk_slot[gid] = S.sch.slot(gid)
            elif ev[0] == "cpu":
                S.sch.host_done(ev[1], self.env.now)
            elif ev[0] == "tick" and S.sch.idle and not S.sch.finished:
                # Nothing moves but arrivals: a drop after the last chunk seen shows only here.
                st = yield from S.ing_status.read()
                S.sch.on_status(int(st.chunks), int(st.overruns), self.env.now)
            lt_done = all(S.sch.state[g] == "done" for g in range(len(S.lt.jobs)))
            if not rt_added and lt_done:
                rt_added = True
                S.rt = real_time(
                    S.d, t0=self.env.now
                )  # deadlines from the slot's real start
                S.rt_base = S.sch.add(S.rt)
                S.oru.start.succeed()
                self.start(self._ticker, name="ticker")
            yield from self._pump()
        self.done.succeed()

    def _ticker(self):
        while not self.done.triggered:
            yield self.env.timeout(T_SYM)
            yield from self.inbox.put(("tick",))

    def _resp_reader(self):
        while True:
            r = yield from self.sys.qresp.get_schema(Resp)
            yield from self.inbox.put(("resp", r))

    def _cpu_worker(self):
        while True:
            gid = yield from self.cpu_q.get()
            yield from self._run_cpu(gid)
            yield from self.inbox.put(("cpu", gid))

    def _pump(self):
        """Everything the scheduler lets happen now: Python stages, CPU jobs, engine jobs, credit."""
        S = self.sys
        sch = S.sch
        while True:
            gid = sch.take_host()
            if gid is None:
                break
            yield from self._charge_trace()
            if sch.jobs[gid].engine == "py":
                yield from self._run_py(gid)
                sch.host_done(gid, self.env.now)
            else:
                yield from self.cpu_q.put(gid)
        for e in ("sys", "vec"):
            room = S.capacity[e] - sum(1 for (e2, _tx) in sch.in_flight if e2 == e)
            for act in sch.take(e, max(0, room)):
                if act[0] == "job":
                    words = S.descriptor(act[1], act[2])
                    self.jobs_log[e].append(words)
                    yield from S.qjob[e].write(words)
        if sch.credit != S.credit_sent:
            S.credit_sent = sch.credit
            yield from self.m.write(np.asarray([sch.credit], np.uint64), S.credit_addr)
        yield from self._charge_trace()

    # -- the host's jobs ------------------------------------------------------------------------

    def _ctx(self, gid: int) -> dict:
        """A host job's record for the replay file: its bus writes and charged host cycles."""
        j = self.sys.sch.jobs[gid]
        ctx = {"key": list(j.key), "op": j.op, "writes": [], "cycles": 0}
        self.replay.append(ctx)
        return ctx

    def _run_py(self, gid: int):
        S = self.sys
        j = S.sch.jobs[gid]
        ctx = self._ctx(gid)
        if j.op == "srs":
            for name in ("Hah", "Ham"):
                yield from self.write_words(S.addr(gid, name), S.ha_words[name], ctx)
        elif j.op == "init":
            q = j.key[1]
            x, r_, d = (f"{v}{q}@0" for v in "XRD")
            for name, words in zip((x, r_, d), S.init_words(q), strict=True):
                yield from self.write_words(S.addr(gid, name), words, ctx)
        elif j.op == "dmrs":
            yield from S.run_dmrs(self, gid, ctx)
        elif j.op == "meas":
            _m, p, c = j.key
            name = f"xh{p}:{c}"
            got = yield from self.read_words(
                S.addr(gid, name), S.lt_rt_words(gid, name)
            )
            self.results["xh"][p, c] = got
        else:
            raise ValueError(f"no Python stage {j.op!r}")

    def _run_cpu(self, gid: int):
        S = self.sys
        j = S.sch.jobs[gid]  # ("proj", k): k the UE's index among the sector's own
        yield from S.run_proj(self, gid, j.key[1], self._ctx(gid))


# -- the system ----------------------------------------------------------------------------------------


class LtbfSystem:
    """The built system: its parts, the plan, and the descriptors (see :func:`build`)."""

    def __init__(self, inp: Inputs) -> None:
        self.inp, self.p, self.cfg, self.d = inp, inp.p, inp.cfg, inp.dims
        p, d, cfg = self.p, self.d, self.cfg
        self.sch = Scheduler(policy=p.policy, qresp_depth=p.qresp_depth,
                             ring_depth=p.ring_symbols * d.n_chunks, n_chunks=d.n_chunks)  # fmt: skip
        self.lt = long_term(d)
        self.rt = real_time(d)
        self.rt_base = None
        self.chunk_slot: dict[int, int] = {}
        self.credit_sent = 0
        self.f = cfg.cg_step()
        self.vu, self.off = pg.system_config(self.f, cfg.eq, k=d.rows, n=d.c, u_pad=d.u_pad,
                                             r=p.r, L=p.L, word_bits=p.word_bits,
                                             lane_bits=p.lane_bits, s_max=max(4, inp.sh),
                                             m_max=max(8, cfg.m_shift))  # fmt: skip
        self._layout()

    # -- memory -------------------------------------------------------------------------------

    def w(self, rows: int, cols: int) -> int:
        return self.d.words(rows, cols)

    def _layout(self) -> None:
        p, d, inp, cfg = self.p, self.d, self.inp, self.cfg
        progs = {
            "cg": pg.cg_step_programs(self.vu, d.rows, d.c, inp.sh),
            "cg_last": pg.cg_step_programs(
                self.vu, d.rows, d.c, inp.sh, last_m=cfg.m_shift
            ),
            "eq": pg.eq_programs(self.vu, d.u_pad, d.c, p.r, d.J, off=self.off),
        }
        addr = 0
        self.programs: dict[str, tuple[int, np.ndarray]] = {}
        self.prog_at: dict[str, dict[str, tuple[int, int]]] = {}
        for name, pr in progs.items():
            words = assemble(self.vu, pr, tx_id=0)
            self.prog_at[name] = {}
            for part in ("load", "execute", "store"):
                wds = np.asarray(words[part], np.uint64)
                self.programs[f"{name}.{part}"] = (addr, wds)
                self.prog_at[name][part] = (addr, len(wds))
                addr += len(wds)
        self.gp_addr = addr
        addr += self.w(d.rows, d.S)
        self.ring_addr = addr
        self.slot_words = (d.rows // d.c) * d.J * self.w(d.c, d.c)
        addr += self.sch.ring_depth * self.slot_words
        self.lt_plan = plan_memory(self.lt, 1 << 30, base=addr)
        addr += footprint(self.lt, self.lt_plan, base=addr)
        self.rt_plan = plan_memory(self.rt, 1 << 30, base=addr)
        addr += footprint(self.rt, self.rt_plan, base=addr)
        self.mem_words = addr
        # The Python stages' operands.
        hr, hi = (
            np.pad(v, ((0, d.rows - v.shape[0]), (0, d.cols - v.shape[1])))
            for v in inp.h
        )
        self.ha_words = {
            "Hah": block_major(hr, hi, cfg.a, d.c, d.Mmax, lane_bits=p.lane_bits,
                               word_bits=p.word_bits, order="col"),
            "Ham": block_major(hr, hi, cfg.a, d.Mmax, d.c, lane_bits=p.lane_bits,
                               word_bits=p.word_bits, order="row"),
        }  # fmt: skip
        self.gp_stage = (
            np.zeros((d.rows, d.S), np.int64),
            np.zeros((d.rows, d.S), np.int64),
        )

    def addr(self, gid: int, name: str) -> int:
        """A buffer's word address for job *gid*'s graph."""
        if name == "Gp":
            return self.gp_addr
        plan = self.lt_plan if gid < len(self.lt.jobs) else self.rt_plan
        return plan[name]

    def lt_rt_words(self, gid: int, name: str) -> int:
        g = self.lt if gid < len(self.lt.jobs) else self.rt
        return g.buffers[name]

    def to_w(self, re, im, fmt) -> np.ndarray:
        return np.asarray(
            to_words(re, im, fmt, self.p.lane_bits, self.p.word_bits), np.uint64
        )

    def from_w(self, words, n: int, fmt):
        return from_words(words, n, fmt, self.p.lane_bits, self.p.word_bits)

    def init_words(self, q: int) -> list[np.ndarray]:
        """CG job *q*'s ``X₀ = 0``, ``R₀ = q_R(B')``, ``D₀ = q_P(R₀)`` (``linalg.cg.cg_init``)."""
        from waveflow.linalg import cg as lcg

        d, f = self.d, self.f
        br, bi = (np.pad(v, ((0, d.rows - v.shape[0]), (0, d.n_cg * d.c - v.shape[1])))
                  for v in self.inp.b)  # fmt: skip
        cols = slice(q * d.c, (q + 1) * d.c)
        st = lcg.cg_init(br[:, cols], bi[:, cols], self.cfg.cg)
        return [self.to_w(st.xr, st.xi, f.X), self.to_w(st.rr, st.ri, f.R),
                self.to_w(st.pr, st.pi, f.D)]  # fmt: skip

    # -- descriptors --------------------------------------------------------------------------

    def descriptor(self, gid: int, tx: int) -> np.ndarray:
        j = self.sch.jobs[gid]
        if j.engine == "sys":
            return self._sys_desc(gid, j, tx)
        return self._vec_desc(gid, j, tx)

    def _b_addr(self, gid: int, name: str) -> int:
        if name.startswith("Y"):  # a ring slot: the chunk root is this job's dependency
            j = self.sch.jobs[gid]
            base = gid - j.jid
            ch = next(
                base + x for x in j.deps if self.sch.jobs[base + x].engine == "ingest"
            )
            return self.ring_addr + self.chunk_slot[ch] * self.slot_words
        return self.addr(gid, name)

    def _sys_desc(self, gid: int, j, tx: int) -> np.ndarray:
        d = self.d
        a, b = j.reads
        (c,) = j.writes
        op = MatmulOp.MUL if j.op == "MUL" else MatmulOp.MUL_AH
        M, K, N, r0 = (j.shape[k] for k in ("M", "K", "N", "row0"))
        P, NT = K // d.c, N // d.c
        a_off = (r0 // d.Mmax) * P * self.w(d.Mmax, d.c)
        c_off = (r0 // d.Mmax) * NT * self.w(d.Mmax, d.c)
        desc = EngineJobDesc(tx_id=tx, op=int(op), M=M, K=K, N=N, a_base=self.addr(gid, a) + a_off,
                             b_base=self._b_addr(gid, b), c_base=self.addr(gid, c) + c_off)  # fmt: skip
        return np.asarray(desc.serialize(word_bw=self.p.word_bits), np.uint64)

    def _vec_desc(self, gid: int, j, tx: int) -> np.ndarray:
        d, p = self.d, self.p
        if j.op == "step":
            prog = self.prog_at["cg_last" if j.shape["last"] else "cg"]
            mw = self.w(d.rows, d.c)
            lds = [(self.addr(gid, b), 0, mw) for b in j.reads]
            sts = [(self.addr(gid, b), 0, mw) for b in j.writes]
            count = 1
        else:  # eq: J rounds, each tile's w_r and z_r blocks, x̂ a tile
            prog = self.prog_at["eq"]
            u_pad = d.u_pad
            wb = self.w(u_pad, d.c)
            wt = self.w(
                d.S, d.c
            )  # a C tile of Z (and w): S rows, r blocks of U_pad, then padding
            zb, wbase = (self.addr(gid, b) for b in j.reads)  # (Z, w)
            lds = []
            for k in range(p.r):
                lds += [(wbase + k * wb, wt, wb), (zb + k * wb, wt, wb)]
            sts = [(self.addr(gid, j.writes[0]), wb, wb)]
            count = d.J
        job = VecMemJob(tx_id=tx, count=count, n_ld=len(lds), n_st=len(sts),
                        l_addr=prog["load"][0], l_words=prog["load"][1],
                        x_addr=prog["execute"][0], x_words=prog["execute"][1],
                        s_addr=prog["store"][0], s_words=prog["store"][1])  # fmt: skip
        for k, (base, stride, n) in enumerate(lds):
            setattr(job, f"ld{k}_base", base), setattr(job, f"ld{k}_stride", stride)
            setattr(job, f"ld{k}_words", n)
        for k, (base, stride, n) in enumerate(sts):
            setattr(job, f"st{k}_base", base), setattr(job, f"st{k}_stride", stride)
            setattr(job, f"st{k}_words", n)
        return np.asarray(job.serialize(word_bw=p.word_bits), np.uint64)

    # -- the host's computations --------------------------------------------------------------

    def run_proj(self, host: LtbfHost, gid: int, k: int, ctx: dict):
        """UE *k*'s CPU job: read its CG job's ``W`` and ``X`` over the bus, ``project_ue`` (the chain's
        own code), charge the A53, write its ``G'`` columns of ``G'_stackᴴ``."""
        d, cfg, inp = self.d, self.cfg, self.inp
        j = self.sch.jobs[gid]
        wname, xname = j.reads
        q = 8 * k // d.c
        ww = yield from host.read_words(self.addr(gid, wname), self.lt.buffers[wname])
        xw = yield from host.read_words(self.addr(gid, xname), self.lt.buffers[xname])
        wr, wi = self.from_w(ww, d.cols * d.c, cfg.bc)
        xr, xi = self.from_w(xw, d.rows * d.c, self.f.X)
        n_conn = d.U_conn or d.U
        cols = slice(8 * k - q * d.c, 8 * k - q * d.c + 8)
        w = lf.to_complex(wr.reshape(d.cols, d.c), wi.reshape(d.cols, d.c), cfg.bc)
        x = lf.to_complex(xr.reshape(d.rows, d.c), xi.reshape(d.rows, d.c), self.f.X)
        wsc = w[: 8 * n_conn, cols] * 2.0 ** (inp.sh + cfg.m_shift)
        u = inp.own[k]
        g = fc.project_ue(wsc, x[: d.n_rx, cols], inp.prep.pos[u], self.p.r)
        gr, gi = lf.to_stored(g.conj().T * 2.0**-cfg.g_shift, cfg.a)
        yield from host.charge(host.proj_fn, ctx, **ltbf_proj.proj_counters(
            wsc, d.n_rx, inp.prep.pos[u], self.p.r))  # fmt: skip
        yield from host.gp_lock.acquire()
        u_pad = d.u_pad
        for s in range(self.p.r):
            self.gp_stage[0][: d.n_rx, s * u_pad + k] = gr[:, s]
            self.gp_stage[1][: d.n_rx, s * u_pad + k] = gi[:, s]
        words = block_major(*self.gp_stage, cfg.a, d.c, d.Mmax, lane_bits=self.p.lane_bits,
                            word_bits=self.p.word_bits, order="col")  # fmt: skip
        yield from host.write_words(self.gp_addr, words, ctx)
        host.gp_lock.release()

    def run_dmrs(self, host: LtbfHost, gid: int, ctx: dict):
        """The slot's DMRS stage: read symbols 2-3's ``Z`` chunks, ``chest.dmrs_weights`` per UE on
        ``2^(g+k) Z`` (the chain's code), write ``2^(g+k) w`` as every chunk's ``w`` tiles.
        """
        d, cfg, inp, p = self.d, self.cfg, self.inp, self.p
        n = d.J * d.c
        nf = d.n_chunks * n
        z = {
            s: (np.zeros((d.S, nf), np.int64), np.zeros((d.S, nf), np.int64))
            for s in sg.DMRS_SYMS
        }
        for s in sg.DMRS_SYMS:
            for c in range(d.n_chunks):
                name = f"Z{s}:{c}"
                words = yield from host.read_words(
                    self.addr(gid, name), self.rt.buffers[name]
                )
                zr, zi = self._tiles_from(words, d.S, n)
                z[s][0][:, c * n : (c + 1) * n], z[s][1][:, c * n : (c + 1) * n] = (
                    zr,
                    zi,
                )
        host.results["z_dmrs"] = z
        u_pad = d.u_pad
        wr = np.zeros((d.S, nf), np.int64)
        wi = np.zeros((d.S, nf), np.int64)
        for k, u in enumerate(inp.own):
            rows = [s * u_pad + k for s in range(p.r)]
            zdr = np.stack([z[s][0][rows, : sg.N_SC] for s in sg.DMRS_SYMS], axis=1)
            zdi = np.stack([z[s][1][rows, : sg.N_SC] for s in sg.DMRS_SYMS], axis=1)
            zc = lf.to_complex(zdr, zdi, cfg.bc) * inp.back
            _h, w = ce.dmrs_weights(zc, int(inp.prep.ports[u]), p.sector, inp.prep.log)
            ur, ui = lf.to_stored(w * inp.back, cfg.eq.w)
            wr[rows, : sg.N_SC], wi[rows, : sg.N_SC] = ur, ui
            host.results["w"][u] = (ur, ui)
        for c in range(d.n_chunks):
            name = f"w:{c}"
            words = self._tiles_to(
                wr[:, c * n : (c + 1) * n], wi[:, c * n : (c + 1) * n], cfg.eq.w
            )
            yield from host.write_words(self.addr(gid, name), words, ctx)

    def _tiles_from(self, words, rows: int, n: int):
        """A chunk stored as C tiles of ``rows x c`` (``rows`` = ``Mmax``): its ``rows x n`` integers."""
        d = self.d
        tw = self.w(rows, d.c)
        fmt = self.cfg.bc
        re, im = np.zeros((rows, n), np.int64), np.zeros((rows, n), np.int64)
        for j in range(n // d.c):
            tr, ti = self.from_w(words[j * tw : (j + 1) * tw], rows * d.c, fmt)
            re[:, j * d.c : (j + 1) * d.c] = tr.reshape(rows, d.c)
            im[:, j * d.c : (j + 1) * d.c] = ti.reshape(rows, d.c)
        return re, im

    def _tiles_to(self, re, im, fmt) -> np.ndarray:
        d = self.d
        return np.concatenate([self.to_w(re[:, j * d.c : (j + 1) * d.c], im[:, j * d.c : (j + 1) * d.c],
                                         fmt) for j in range(re.shape[1] // d.c)])  # fmt: skip


def build(inp: Inputs) -> tuple[HwSystem, LtbfSystem, LtbfHost]:
    """The pysim system for *inp* (see the module docstring)."""
    S = LtbfSystem(inp)
    p, d, cfg = S.p, S.d, S.cfg
    W = p.word_bits
    sim = Simulation()
    clk = Clock(freq=F_HOST)
    platform = CpuPlatform.load()
    cpu = Processor(name="cpu", sim=sim, config=platform.cpu_config(n_cores=p.n_cores))
    host = LtbfHost(name="host", sim=sim, clk=clk, cpu=cpu, platform=platform)
    # The A53 is the host's own resource, not a testbench participant: under XSI the host's C++ twin
    # charges the same cycles with compute(n), so the Processor needs no lowering of its own.
    cpu.parent = host
    host.sys = S
    pmax = max(d.rows, d.cols) // d.c
    sysw = SysWrap(name="sys", sim=sim, clk=clk, engine_cfg={
        "word_bits": W, "Mmax": d.Mmax, "Kmax": d.c, "Nmax": d.c, "Pmax": pmax, "R": 4, "C": 8,
        "L": 4, "a": cfg.a, "b": cfg.bc, "c": cfg.bc, "pysim": "tiles", "lane_bits": p.lane_bits})  # fmt: skip
    vecw = VecWrap(name="vec", sim=sim, clk=clk, vu_cfg=S.vu, word_bits=W)
    ing = IngestDev(name="ing", sim=sim, clk=clk, dma_cfg={
        "word_bits": W, "lane_bits": p.lane_bits, "Kmax": d.c, "Nmax": d.c, "n_ant": d.rows,
        "J": d.J, "n_sc": sg.N_SC})  # fmt: skip
    hub = RespHub(name="hub", sim=sim, clk=clk, n_src=3, word_bits=W)
    yr, yi = inp.y
    symbols = [[S.to_w(yr[a, s], yi[a, s], cfg.bc) for a in range(d.n_rx)]
               for s in range(sg.N_SYM)]  # fmt: skip
    oru = OruSource(name="oru", sim=sim, clk=clk, symbols=symbols, word_bits=W,
                    period=p.sym_period)  # fmt: skip
    S.oru = oru
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk,
                    nwords_tot=S.mem_words, addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(S.mem_words) == 0
    S.mem = mem
    devs = {
        "sys": (build_mm_device(sysw, sim=sim, clk=clk, mem_dwidth=W, prefix="sys_"), SysWrap, SYS_BASE),
        "vec": (build_mm_device(vecw, sim=sim, clk=clk, mem_dwidth=W, prefix="vec_"), VecWrap, VEC_BASE),
        "ing": (build_mm_device(ing, sim=sim, clk=clk, mem_dwidth=W, prefix="ing_"), IngestDev, ING_BASE),
        "hub": (build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_"), RespHub, HUB_BASE),
    }  # fmt: skip
    links = []
    for name, m, s in (("sym", oru.m_out, ing.s_sym), ("resp_sys", sysw.m_resp, hub.s_in0),
                       ("resp_vec", vecw.m_resp, hub.s_in1), ("resp_ing", ing.m_resp, hub.s_in2)):  # fmt: skip
        li = StreamIF(name=name, sim=sim, clk=clk, bitwidth=W)
        li.bind("master", m)
        li.bind("slave", s)
        links.append(li)
    masters = [host.m, sysw.m_in, sysw.m_out, vecw.m_rl, vecw.m_rx, vecw.m_rs, vecw.m_rd, vecw.m_w,
               ing.m_w]  # fmt: skip
    slaves, ranges = [], []
    for dev, _cls, base in devs.values():
        sl, rg = dev.ranges(base)
        slaves += sl
        ranges += rg
    slaves.append(mem.s_mm)
    ranges.append((0, S.mem_words * W // 8))
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)
    maps = {
        k: MemSlaveLayout.of(cls, mem_dwidth=W).at(base)
        for k, (_d, cls, base) in devs.items()
    }
    for key, dev_key, view in (("qjob_sys", "sys", "qjob"), ("qjob_vec", "vec", "qjob"),
                               ("qresp", "hub", "qresp")):  # fmt: skip
        line = IrqIF(name=f"{key}_irq", sim=sim)
        line.bind("source", devs[dev_key][0].views[view].m_irq)
        line.bind("sink", host.irq[key])
    ad = {k: BoundMemSlaveAdaptor(m, host.m) for k, m in maps.items()}
    S.qjob = {
        e: ad[e].stream_master("qjob", irq=host.irq[f"qjob_{e}"])
        for e in ("sys", "vec")
    }
    S.qresp = ad["hub"].stream_slave("qresp", irq=host.irq["qresp"])
    S.ing_ctrl = ad["ing"].stream_master("ctrl")
    S.ing_status = ad["ing"].status("ctrl")
    S.credit_addr = maps["ing"]["credit"].base
    S.ing_cfg = IngestCfg(enable=1, ring_depth=S.sch.ring_depth, ring_base=S.ring_addr,
                          chunk_words=S.slot_words)  # fmt: skip
    # Jobs an engine may have unanswered: what its Job FIFO holds (a descriptor and its length word
    # a slot) and the one it is running.  A queue write past a full FIFO waits on the view's room
    # interrupt, so it never stalls the bus.
    desc = {
        "sys": len(EngineJobDesc().serialize(word_bw=W)),
        "vec": len(VecMemJob().serialize(word_bw=W)),
    }
    S.capacity = {e: qd // (desc[e] + 1) + 1 for e, qd in (("sys", 16), ("vec", 32))}
    dut = DutTop.of("ltbf_dut", sim, comps=[d_[0] for d_ in devs.values()] + [mem, oru],
                    ifs=[xbar, *links])  # fmt: skip
    hw = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.results)
    return hw, S, host


def check(S: LtbfSystem, host: LtbfHost) -> dict[str, bool]:
    """Shared Mem and the host's reads against the fixed-point chain (``golden``), bit for bit."""
    d, cfg, inp, p = S.d, S.cfg, S.inp, S.p
    gold = inp.gold
    u_pad = d.u_pad
    out = {}
    words = np.asarray(S.mem._mem.read(S.gp_addr * 8, S.w(d.rows, d.S)), np.uint64)
    gr = np.zeros((d.rows, d.S), np.int64)
    gi = np.zeros((d.rows, d.S), np.int64)
    for k, u in enumerate(inp.own):
        for s in range(p.r):
            gr[: d.n_rx, s * u_pad + k], gi[: d.n_rx, s * u_pad + k] = (
                gold["g"][u][0][:, s],
                gold["g"][u][1][:, s],
            )
    out["G'"] = np.array_equal(words, block_major(gr, gi, cfg.a, d.c, d.Mmax, lane_bits=p.lane_bits,
                                                  word_bits=p.word_bits, order="col"))  # fmt: skip
    z = host.results["z_dmrs"]
    ok_z = ok_w = True
    for k, u in enumerate(inp.own):
        for si, s in enumerate(sg.DMRS_SYMS):
            for q in range(p.r):
                row = q * u_pad + k
                ok_z &= np.array_equal(z[s][0][row, : sg.N_SC], gold["z"][u][0][q, s])
                ok_z &= np.array_equal(z[s][1][row, : sg.N_SC], gold["z"][u][1][q, s])
        wr, wi = host.results["w"][u]
        ok_w &= np.array_equal(wr, gold["w"][u][0]) and np.array_equal(
            wi, gold["w"][u][1]
        )
    out["Z (DMRS)"], out["w"] = bool(ok_z), bool(ok_w)
    n = d.J * d.c
    ok_x = len(host.results["xh"]) == len(sg.DATA_SYMS) * d.n_chunks
    for (sym, c), words in host.results["xh"].items():
        di = list(sg.DATA_SYMS).index(sym)
        for j in range(d.J):
            tw = S.w(u_pad, d.c)
            tr, ti = S.from_w(words[j * tw : (j + 1) * tw], u_pad * d.c, cfg.eq.xh)
            tr, ti = tr.reshape(u_pad, d.c), ti.reshape(u_pad, d.c)
            f0 = c * n + j * d.c
            for k, u in enumerate(inp.own):
                xr, xi = gold["xh"][u]
                hi = min(d.c, sg.N_SC - f0)
                if hi <= 0:
                    continue
                ok_x &= np.array_equal(tr[k, :hi], xr[di, f0 : f0 + hi])
                ok_x &= np.array_equal(ti[k, :hi], xi[di, f0 : f0 + hi])
    out["x̂"] = bool(ok_x)
    return out


def write_replay(host: LtbfHost, path) -> dict:
    """The file the RTL gate's C++ host replays (spec §5.7): in run order, every host job's bus writes
    (word address, words) and charged host cycles, and the scheduler's own charges.  ``<path>.json``
    holds the index, ``<path>.npy`` every written word back to back."""
    import json
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    words, jobs, off = [], [], 0
    for ctx in host.replay:
        writes = []
        for addr, w in ctx["writes"]:
            writes.append([int(addr), len(w), off])
            words.append(np.asarray(w, np.uint64))
            off += len(w)
        jobs.append({"key": ctx["key"], "op": ctx["op"], "cycles": int(ctx["cycles"]),
                     "writes": writes})  # fmt: skip
    index = {
        "jobs": jobs,
        "sched_cycles": [int(c) for c in host.sched_cycles],
        "words": off,
    }
    path.with_suffix(".json").write_text(json.dumps(index))
    np.save(
        path.with_suffix(".npy"),
        np.concatenate(words) if words else np.zeros(0, np.uint64),
    )
    return index
