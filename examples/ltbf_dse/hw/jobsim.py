"""jobsim.py -- the job-level timing simulator (plan §9 d, step 5.5).

``plans/ltbf_dse/ltbf_dse.md`` §9 d, AC11.  The pysim system (:mod:`~examples.ltbf_dse.hw.system`)
moves every word; the DSE needs thousands of runs, so this simulator keeps the system's decisions and
drops its data: the same :class:`~examples.ltbf_dse.hw.sched.Scheduler` on the same job graphs,
driven by a host with ``LtbfHost``'s threads -- the dispatcher, the Resp reader, the CPU workers, the
O-RU and the ticker -- whose charges are priced on the same
:class:`~waveflow.cpu.processor.Processor` with the same functions; every bus operation, queue
write and engine job is a timed wait (:class:`Timing`), and the Ingest DMA keeps its ring as the
device does (a symbol whose first word finds fewer than ``n_chunks`` free slots is dropped).

:class:`Timing` holds the models.  The host's operations are pysim's (measured at AC9: a read of
``n`` words ``n + 2`` cycles, a write about ``17 + 1.03 n``, a queue write ``n + 3`` once there is
room, a status read 3); each engine job's service time and each chunk's arrival after its symbol's
start are its methods.  :func:`oracle` builds a :class:`Timing` that replays a pysim run's own job
times and A53 charges, which checks this simulator's event logic alone.

    python -m examples.ltbf_dse.hw.jobsim          # AC9 against its pysim run (the oracle)
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import simpy

from examples.ltbf_dse.hw.graphs import Dims, long_term, real_time
from examples.ltbf_dse.hw.records import SRC_INGEST, SRC_SYS, SRC_VEC
from examples.ltbf_dse.hw.sched import Scheduler

F_HOST = 250e6
T_SYM = 125e-6 / 14
SRC = {"sys": SRC_SYS, "vec": SRC_VEC}
MAX_BURST = 256


@dataclass
class Timing:
    """The simulator's models, in host cycles (see the module docstring).  ``sys_job`` and
    ``vec_job`` are ``(gid, job) -> cycles``; ``chunk`` ``(k) -> cycles`` after the symbol's start
    for its ``k``-th chunk's ``Resp``; ``proj`` ``(gid) -> A53 cycles`` (``None``: priced by the
    projection's model on :attr:`proj_counters`)."""

    sys_job: Callable[[int, Any], float]
    vec_job: Callable[[int, Any], float]
    chunk: Callable[[int], float]
    proj: Callable[[int], float] | None = None
    #: The projection model's Jacobi counters when :attr:`proj` is ``None``.
    proj_counters: dict = field(default_factory=lambda: {"n_rot": 168.0, "n_sweeps": 6.0})
    read: tuple = (2.0, 1.0)  # a + b n
    write: tuple = (17.4, 1.03)
    queue: tuple = (3.0, 1.0)
    status: float = 3.0
    #: An engine's Resp record reaching the host's reader (the hub, the queue view, its interrupt).
    resp: float = 3.0

    def reads(self, n: int) -> float:
        return sum(self.read[0] + self.read[1] * min(MAX_BURST, n - i) for i in range(0, n, MAX_BURST))

    def writes(self, n: int) -> float:
        return sum(self.write[0] + self.write[1] * min(MAX_BURST, n - i)
                   for i in range(0, n, MAX_BURST))  # fmt: skip


@dataclass
class Config:
    """What a run needs besides the models: the graphs' dimensions and the system's parameters
    (``SysParams``'s of the same names)."""

    d: Dims
    policy: str = "static"
    qresp_depth: int = 64
    ring_symbols: int = 2
    n_cores: int = 1
    sym_period: float = T_SYM
    word_bits: int = 64
    #: Jobs each engine may have unanswered: its Job FIFO's descriptors and the one it runs.
    cap: dict = field(default_factory=lambda: {"sys": 5, "vec": 2})
    #: The vector unit's programs, written once before the first job.
    setup_words: int = 173


class JobSim:
    """One run: a long-term update, then one slot (as :func:`system.build`'s host)."""

    def __init__(self, cfg: Config, timing: Timing) -> None:
        from waveflow.cpu.platform import CpuPlatform
        from waveflow.cpu.processor import Processor
        from waveflow.simulation.simulation import Simulation

        self.cfg, self.t = cfg, timing
        d = cfg.d
        self.sim = Simulation()
        self.env: simpy.Environment = self.sim.env
        self.platform = CpuPlatform.load()
        self.cpu = Processor(name="cpu", sim=self.sim, config=self.platform.cpu_config(n_cores=cfg.n_cores))
        self.period = 1.0 / F_HOST
        self.sch = Scheduler(policy=cfg.policy, qresp_depth=cfg.qresp_depth,
                             ring_depth=cfg.ring_symbols * d.n_chunks, n_chunks=d.n_chunks)  # fmt: skip
        self.lt = long_term(d)
        self.rt = None
        self._fns = self._functions()
        self._traced = 0
        self.inbox = simpy.Store(self.env)
        self.cpu_q = simpy.Store(self.env)
        self.gp_lock = simpy.Resource(self.env, capacity=1)
        self.fifo = {e: simpy.Store(self.env, capacity=max(1, cfg.cap[e] - 1)) for e in ("sys", "vec")}
        self.slot_start = self.env.event()
        self.done = self.env.event()
        self.t_start: dict[int, float] = {}
        # The Ingest DMA's ring, as the device keeps it.
        self.credit_seen = 0
        self.taken = self.seq = self.chunks = self.overruns = 0
        self.credit_sent = 0
        self.t_lt = math.nan

    # -- the A53 --------------------------------------------------------------------------------

    def _functions(self) -> dict:
        from examples.ltbf_dse import a53cal, ltbf_proj, sched_release
        from examples.ltbf_dse.hw.system import _Linear, _ProvisionalProj
        from waveflow.cpu.task import SwFunction

        def fn(name, cycles):
            return SwFunction(name=name, fn=lambda **kw: (None, kw), cycles=cycles)

        pf = self.platform
        rel = a53cal.load_model(sched_release.CAL)
        proj = a53cal.load_model(ltbf_proj.CAL)
        out = {f: SwFunction(name=f, fn=lambda **kw: (None, kw), cycles=pf.model(f, "cycles"),
                             energy_pj=pf.model(f, "energy_pj"))
               for f in ("sched_ops.add", "sched_ops.delete", "dispatch")}  # fmt: skip
        out["release"] = fn("release", rel or _Linear(20.0, {"n_succ": 12.0}))
        out["ctrl"] = SwFunction.fixed("ctrl", 40.0)
        out["proj"] = fn("ltbf_proj", proj or _ProvisionalProj())
        return out

    def charge(self, func, **kw):
        """As ``LtbfHost.charge``: the Processor's time, padded to whole host cycles."""
        yield from self.cpu.execute(func, **kw)
        rec = self.cpu.records[-1]
        n = math.ceil((rec.cycles + rec.switch_cycles) * F_HOST / self.cpu.config.f_clk_hz - 1e-9)
        pad = rec.t_start + n * self.period - self.env.now
        if pad > 0:
            yield self.env.timeout(pad)

    def _charge_trace(self):
        tr = self.sch.trace
        while self._traced < len(tr):
            t = dict(tr[self._traced])
            self._traced += 1
            op = t.pop("op")
            t.pop("engine", None)
            yield from self.charge(self._fns[op], **{k: float(v) for k, v in t.items()})

    def wait(self, cycles: float):
        if cycles > 0:
            yield self.env.timeout(cycles * self.period)

    # -- the host's threads (LtbfHost's) ---------------------------------------------------------

    def run(self) -> dict:
        env = self.env
        env.process(self._main())
        env.process(self._oru())
        for k in range(self.cfg.n_cores):
            env.process(self._cpu_worker())
        for e in ("sys", "vec"):
            env.process(self._engine(e))
        env.run(until=self.done)
        sch = self.sch
        return {"end": env.now, "t_lt": self.t_lt, "overruns": sch.overruns, "dropped": sch.dropped,
                "misses": dict(sch.misses), "finished": sch.finished}  # fmt: skip

    def _main(self):
        sch, d, t = self.sch, self.cfg.d, self.t
        yield from self.wait(t.writes(self.cfg.setup_words))
        yield from self.wait(t.queue[0] + 2 * t.queue[1])  # the Ingest DMA's Ctrl
        sch.add(self.lt)
        rt_added = False
        yield from self._pump()
        while not (rt_added and sch.finished):
            ev = yield self.inbox.get()
            if ev[0] == "resp":
                _k, src, tx, seq = ev
                sch.on_resp(src, 0, tx, seq, self.env.now)
            elif ev[0] == "cpu":
                sch.host_done(ev[1], self.env.now)
            elif ev[0] == "tick" and sch.idle and not sch.finished:
                yield from self.wait(t.status)
                sch.on_status(self.chunks, self.overruns, self.env.now)
            lt_done = all(sch.state[g] == "done" for g in range(len(self.lt.jobs)))
            if not rt_added and lt_done:
                rt_added = True
                self.t_lt = self.env.now
                self.rt = real_time(d, t0=self.env.now)
                sch.add(self.rt)
                self.slot_start.succeed()
                self.env.process(self._ticker())
            yield from self._pump()
        self.done.succeed()

    def _pump(self):
        sch, t = self.sch, self.t
        while True:
            gid = sch.take_host()
            if gid is None:
                break
            yield from self._charge_trace()
            if sch.jobs[gid].engine == "py":
                yield from self._run_py(gid)
                sch.host_done(gid, self.env.now)
            else:
                yield self.cpu_q.put(gid)
        for e in ("sys", "vec"):
            room = self.cfg.cap[e] - sum(1 for (e2, _tx) in sch.in_flight if e2 == e)
            for act in sch.take(e, max(0, room)):
                if act[0] == "job":
                    words = 3 if e == "sys" else 16  # EngineJobDesc, VecMemJob at 64 bits
                    yield self.fifo[e].put((act[1], act[2]))  # waits for room, as the queue view
                    yield from self.wait(t.queue[0] + t.queue[1] * words)
        if sch.credit != self.credit_sent:
            self.credit_sent = sch.credit
            yield from self.wait(t.writes(1))
            self.credit_seen = sch.credit
        yield from self._charge_trace()

    def _ticker(self):
        while not self.done.triggered:
            yield self.env.timeout(T_SYM)
            yield self.inbox.put(("tick",))

    def _cpu_worker(self):
        while True:
            gid = yield self.cpu_q.get()
            yield from self._run_cpu(gid)
            yield self.inbox.put(("cpu", gid))

    def _bufs(self, gid: int):
        g = self.lt if gid < len(self.lt.jobs) else self.rt
        return g.buffers

    def _run_py(self, gid: int):
        t = self.t
        j = self.sch.jobs[gid]
        b = self._bufs(gid)
        if j.op in ("srs", "init"):
            for name in j.writes:
                yield from self.wait(t.writes(b[name]))
        elif j.op == "dmrs":
            for name in j.reads:
                yield from self.wait(t.reads(b[name]))
            for name in j.writes:
                yield from self.wait(t.writes(b[name]))
        elif j.op == "meas":
            for name in j.reads:
                yield from self.wait(t.reads(b[name]))
        else:
            raise ValueError(f"no Python stage {j.op!r}")

    def _run_cpu(self, gid: int):
        t, d = self.t, self.cfg.d
        j = self.sch.jobs[gid]
        b = self._bufs(gid)
        for name in j.reads:
            yield from self.wait(t.reads(b[name]))
        if t.proj is not None:
            yield from self.charge(type(self._fns["ctrl"]).fixed("ltbf_proj", t.proj(gid)))
        else:
            yield from self.charge(self._fns["proj"], n=float(d.n_rx), r=float(d.r),
                                   conn=float(d.U_conn or d.U), **t.proj_counters)  # fmt: skip
        with self.gp_lock.request() as req:
            yield req
            yield from self.wait(t.writes(d.words(d.rows, d.S)))

    # -- the engines and the Ingest DMA -----------------------------------------------------------

    def _engine(self, e: str):
        t = self.t
        while True:
            gid, tx = yield self.fifo[e].get()
            self.t_start[gid] = self.env.now
            job = self.sch.jobs[gid]
            yield from self.wait(t.sys_job(gid, job) if e == "sys" else t.vec_job(gid, job))
            yield from self.wait(t.resp)
            yield self.inbox.put(("resp", SRC[e], tx, 0))

    def _oru(self):
        """The O-RU's symbols into the Ingest DMA, which writes a symbol's chunks when its first word
        finds ``n_chunks`` free ring slots, else drops it (counted)."""
        cfg, t, d = self.cfg, self.t, self.cfg.d
        yield self.slot_start
        t0 = self.env.now
        ring = cfg.ring_symbols * d.n_chunks
        ant_words = d.words(1, d.n_sc)
        for s in range(d.n_sym):
            yield self.env.timeout(max(0.0, t0 + (s + 1) * cfg.sym_period - self.env.now))
            start = self.env.now
            if ring - (self.taken - self.credit_seen) < d.n_chunks:
                self.overruns += 1
                self.seq += d.n_chunks
            else:
                self.taken += d.n_chunks
                for k in range(d.n_chunks):
                    self.env.process(self._chunk(start, k, self.seq + k))
                self.seq += d.n_chunks
            yield from self.wait(d.n_rx * (t.queue[0] + t.queue[1] * ant_words))

    def _chunk(self, start: float, k: int, seq: int):
        yield self.env.timeout(max(0.0, start + self.t.chunk(k) * self.period - self.env.now))
        self.chunks += 1
        yield self.inbox.put(("resp", SRC_INGEST, 0, seq % (1 << 16)))


# -- the oracle: a pysim run's own times ---------------------------------------------------------------


def oracle(inp) -> tuple[Config, Timing, dict]:
    """``(Config, Timing, pysim)`` for *inp* (``system.inputs``): its pysim run's engine service
    times, each chunk's arrival after its symbol's start and each projection's charged A53 cycles
    replayed; ``pysim`` its end and long-term end (seconds)."""
    from examples.ltbf_dse.hw import system as sy

    hw, S, host = sy.build(inp)
    disp: dict[int, float] = {}
    old = S.descriptor

    def desc(gid, tx):
        disp[gid] = host.env.now
        return old(gid, tx)

    S.descriptor = desc
    hw.run()
    sch, p = S.sch, S.p
    serv: dict[int, float] = {}
    for e in ("sys", "vec"):
        prev = 0.0
        for g in sorted((g for g in disp if sch.jobs[g].engine == e), key=lambda g: sch.t_done[g]):
            serv[g] = (sch.t_done[g] - max(disp[g], prev)) * F_HOST
            prev = sch.t_done[g]
    # Chunks: each Resp's time after its symbol's O-RU start (the slot's start plus the symbol's
    # period), by position in the symbol.
    n_lt = len(S.lt.jobs)
    t_lt = max(sch.t_done[g] for g in range(n_lt))
    offs: dict[int, list] = {}
    for g in range(n_lt, len(sch.jobs)):
        j = sch.jobs[g]
        if j.engine == "ingest":
            _c, s, k = j.key
            offs.setdefault(k, []).append((sch.t_done[g] - t_lt - (s + 1) * p.sym_period) * F_HOST)
    proj = {ctx["gid"]: sum(op[1] for op in ctx["ops"] if op[0] == "C") for ctx in host.replay
            if ctx["gid"] >= 0 and sch.jobs[ctx["gid"]].engine == "cpu"}  # fmt: skip
    timing = Timing(sys_job=lambda g, j: serv[g], vec_job=lambda g, j: serv[g],
                    chunk=lambda k: sorted(offs[k])[len(offs[k]) // 2],
                    proj=lambda g: proj[g])  # fmt: skip
    cfg = Config(d=S.d, policy=p.policy, qresp_depth=p.qresp_depth, ring_symbols=p.ring_symbols,
                 n_cores=p.n_cores, sym_period=p.sym_period, word_bits=p.word_bits,
                 cap=dict(S.capacity), setup_words=sum(len(w) for _a, w in S.programs.values()))  # fmt: skip
    return cfg, timing, {"end": host.env.now, "t_lt": t_lt, "replay": host.replay}


if __name__ == "__main__":
    from examples.ltbf_dse.hw import system as sy

    cfg, timing, ref = oracle(sy.inputs(sy.AC9))
    out = JobSim(cfg, timing).run()
    for k in ("end", "t_lt"):
        print(f"{k}: jobsim {out[k] * F_HOST:,.0f} cycles, pysim {ref[k] * F_HOST:,.0f} "
              f"({out[k] / ref[k] - 1:+.2%})")
    print({k: out[k] for k in ("overruns", "dropped", "misses", "finished")})
