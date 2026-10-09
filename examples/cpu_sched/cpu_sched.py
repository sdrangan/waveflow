"""cpu_sched.py — a micro-scheduler on a calibrated A53, dispatching jobs to SimpFun accelerators.

The worked example of ``plans/cpu_model.md`` (step 13): the processor model's first end-to-end user.

* **Arrivals.**  Jobs arrive as a seeded Poisson stream, each with a priority (as task groups arrive
  in tracerspecsense's random-signal simulation).
* **The scheduler is software on the CPU.**  Its ready list is a Python list kept sorted by
  ``(prio, id)``, and every change to it is a call on the :class:`~waveflow.cpu.processor.Processor`:
  an arrival is an ``add``, a dispatch removes the head (``delete``), and a periodic aging pass
  promotes the oldest waiting job (``reprio``).  Each call runs the very algorithm the A53 platform
  was calibrated on (``waveflow/cpu/calib/kernels/sched_ops.py``) on the *real* list, and is priced
  by the platform's fitted cycle and energy models from the counters it reports.  The hand-off to an
  accelerator costs a ``dispatch``.
* **The accelerators are hardware.**  Each :class:`~examples.regmap.simp_fun.SimpFun` sits behind its
  own AXI-Lite link; a dispatcher programs ``x``, ``a``, ``b``, launches it and sleeps on its
  interrupt -- bus time is the bus model's, never the CPU's (the processor's core is free meanwhile).
* **Outputs.**  The processor's report (per-operation latency, queueing, utilization, energy,
  footprint, each with its confidence), every job's result checked against ``relu(a*x + b)``, and
  the scheduler's trace of operations -- the input of the gem5 replay that checks the model
  (``tests/examples/test_cpu_sched_replay.py``).

    python -m examples.cpu_sched.cpu_sched --jobs 200 --accels 2
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import simpy

from examples.regmap.simp_fun import SimpFun, relu_affine
from waveflow.cpu.calib.kernels.sched_ops import (
    _Counters,
    tg_insert,
    tg_remove,
)
from waveflow.cpu.platform import CpuPlatform
from waveflow.cpu.processor import Processor
from waveflow.cpu.report import CpuReport
from waveflow.cpu.task import SwFunction
from waveflow.hw.aximm import DirectMMIF, MMIFMaster
from waveflow.hw.clock import Clock
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

#: The scheduler's operations, by the platform family that prices each.
OPS = ("sched_ops.add", "sched_ops.delete", "sched_ops.reprio")

#: Arrival-time priorities: 1 is the most urgent class.  Aging lowers a waiting job's number by one.
ARRIVAL_PRIOS = (8, 16, 32)


@dataclass
class Job:
    gid: int
    prio: int
    x: int
    a: int
    b: int
    t_arrive: float
    t_done: float = -1.0
    y: int | None = None
    dispatches: int = 0


@dataclass(kw_only=True)
class MicroScheduler(SimObj):
    """The scheduler: a ready list on the CPU, and one dispatcher per accelerator."""

    cpu: Processor
    platform: CpuPlatform
    n_jobs: int = 100
    mean_interarrival_s: float = 0.3e-6
    aging_period_s: float = 10e-6
    seed: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ready: list[tuple[int, int]] = []  # (gid, prio), sorted by (prio, gid)
        self.jobs: dict[int, Job] = {}
        #: Every scheduler operation in the order its body ran: the replay's input.
        self.trace: list[dict[str, Any]] = []
        self.funcs = {op: self._sw(op) for op in OPS}
        self.dispatch_fn = self._dispatch_sw()
        self.accels: list[tuple[SimpFun, Any, IrqIFSink]] = []
        self._work: simpy.Store | None = None
        self._arrived = 0
        self._done = 0

    # -- the software: real list, calibrated prices ------------------------------------------

    def _sw(self, family: str) -> SwFunction:
        # The job's priority travels as ``tg_prio``: ``Processor.execute`` reserves ``prio`` for the
        # scheduling priority of the call itself.
        op = family.split(".")[1]

        def body(**kw: Any) -> tuple[Any, dict[str, float]]:
            c = _Counters()
            n = len(self.ready)
            if op == "add":
                tg_insert(self.ready, (kw["gid"], kw["tg_prio"]), c)
            elif op == "delete":
                # Dispatch removes the HEAD, chosen here -- when the call holds the core -- not by
                # the caller beforehand: two dispatchers waiting for the core would otherwise pick
                # the same head, dispatch it twice and strand another entry.
                gid = self.ready[0][0]
                tg_remove(self.ready, gid, c)
                kw = {"gid": gid}
            else:
                # Aging promotes the TAIL -- the least urgent waiting job -- one step, chosen here at
                # grant for the same reason as the head above.  Nothing to promote: a no-op call.
                kw = {"gid": -1}
                if self.ready and self.ready[-1][1] > 1:
                    gid, prio = self.ready[-1]
                    tg_remove(self.ready, gid, c)
                    tg_insert(self.ready, (gid, prio - 1), c)
                    kw = {"gid": gid, "tg_prio": prio - 1}
            counters = {
                "n_tasks": float(n),
                "n_scanned": float(c.scanned),
                "n_moved": float(c.moved),
            }
            self.trace.append(
                {"op": op, **{k: int(v) for k, v in kw.items()}, **counters}
            )
            return kw.get("gid"), counters

        return SwFunction(
            name=family,
            fn=body,
            cycles=self.platform.model(family, "cycles"),
            energy_pj=self.platform.model(family, "energy_pj"),
            working_set=lambda cnt: 8.0 * (cnt["n_tasks"] + 1),
            code_bytes=self.platform.code_bytes("sched_ops"),
        )

    def _dispatch_sw(self) -> SwFunction:
        return SwFunction(
            name="dispatch",
            fn=lambda: (None, {"n_dispatch": 1.0}),
            cycles=self.platform.model("dispatch", "cycles"),
            energy_pj=self.platform.model("dispatch", "energy_pj"),
            code_bytes=self.platform.code_bytes("dispatch"),
        )

    # -- wiring ----------------------------------------------------------------------------------

    def add_accelerator(self, accel: SimpFun, clk: Clock) -> None:
        i = len(self.accels)
        master = MMIFMaster(name=f"{self.name}_m{i}", sim=self.sim, bitwidth=32)
        link = DirectMMIF(
            sim=self.sim,
            clk=clk,
            byte_addressable=True,
            latency_write=10,
            latency_read=10,
            latency_read_return=10,
        )
        link.bind("master", master)
        link.bind("slave", accel.s_lite)
        sink = IrqIFSink(name=f"{self.name}_irq{i}", sim=self.sim)
        irq = IrqIF(name=f"{self.name}_irqif{i}", sim=self.sim)
        irq.bind("source", accel.s_lite.interrupt())
        irq.bind("sink", sink)
        self.accels.append((accel, accel.regmap.bind_master(master), sink))

    # -- processes -------------------------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None]:
        self._work = simpy.Store(self.env)
        for i in range(len(self.accels)):
            self.env.process(self._dispatcher(i))
        self.env.process(self._aging())
        rng = random.Random(self.seed)
        for gid in range(self.n_jobs):
            yield self.timeout(rng.expovariate(1.0 / self.mean_interarrival_s))
            job = Job(
                gid=gid,
                prio=rng.choice(ARRIVAL_PRIOS),
                x=rng.randint(-50, 50),
                a=rng.randint(-9, 9),
                b=rng.randint(-100, 100),
                t_arrive=self.now,
            )
            self.jobs[gid] = job
            yield from self.cpu.execute(
                self.funcs["sched_ops.add"], gid=gid, tg_prio=job.prio
            )
            self._arrived += 1
            yield self._work.put(gid)

    def _dispatcher(self, i: int) -> ProcessGen[None]:
        _accel, rm, sink = self.accels[i]
        assert self._work is not None
        while True:
            # One token per queued job: a token in hand means the list holds a job for us.
            yield self._work.get()
            gid = yield from self.cpu.execute(self.funcs["sched_ops.delete"])
            yield from self.cpu.execute(self.dispatch_fn)
            job = self.jobs[gid]
            job.dispatches += 1
            yield from rm.set("x", job.x)
            yield from rm.set("a", job.a)
            yield from rm.set("b", job.b)
            yield from rm.run(sink)
            job.y = yield from rm.get("y")
            job.t_done = self.now
            self._done += 1

    def _aging(self) -> ProcessGen[None]:
        """Every period, promote the least urgent waiting job one step, so none starves.

        The aging call runs at scheduling priority 1 (``prio=1``), behind arrivals and dispatches.
        """
        while self._done < self.n_jobs:
            yield self.timeout(self.aging_period_s)
            if not self.ready:
                continue
            yield from self.cpu.execute(self.funcs["sched_ops.reprio"], prio=1)


@dataclass
class RunResult:
    report: CpuReport
    jobs: dict[int, Job]
    trace: list[dict[str, Any]]
    sim_s: float

    @property
    def all_correct(self) -> bool:
        return all(j.y == relu_affine(j.x, j.a, j.b) for j in self.jobs.values())


def run(
    n_jobs: int = 100,
    n_accels: int = 2,
    *,
    n_cores: int = 1,
    seed: int = 1,
    mean_interarrival_s: float = 0.3e-6,
    platform: CpuPlatform | None = None,
) -> RunResult:
    """Build the system, run it to completion, and return the report, jobs and trace."""
    platform = platform or CpuPlatform.load()
    sim = Simulation()
    cpu = Processor(name="cpu", sim=sim, config=platform.cpu_config(n_cores=n_cores))
    sched = MicroScheduler(
        name="sched",
        sim=sim,
        cpu=cpu,
        platform=platform,
        n_jobs=n_jobs,
        mean_interarrival_s=mean_interarrival_s,
        seed=seed,
    )
    clk = Clock(freq=100e6)
    for i in range(n_accels):
        sched.add_accelerator(SimpFun(name=f"simp_fun{i}", sim=sim, clk=clk), clk)
    sim.run_sim()
    wrong = {g: j.dispatches for g, j in sched.jobs.items() if j.dispatches != 1}
    if len(sched.jobs) != n_jobs or wrong:
        raise RuntimeError(
            f"jobs not dispatched exactly once: {wrong or len(sched.jobs)}"
        )
    return RunResult(
        report=cpu.report(), jobs=sched.jobs, trace=sched.trace, sim_s=sim.env.now
    )


def print_report(res: RunResult) -> None:
    rep = res.report
    print(
        f"{len(res.jobs)} jobs in {res.sim_s * 1e6:.1f} us simulated; all correct: {res.all_correct}"
    )
    print(
        f"CPU utilization {[round(u, 3) for u in rep.utilization]}; energy {rep.total_pj / 1e6:.3f} uJ "
        f"(dynamic {rep.dynamic_pj / 1e6:.3f}, static {rep.static_pj / 1e6:.3f})"
    )
    for st in rep.functions.values():
        print(
            f"  {st.name:18s} n {st.n:4d}  mean {st.latency_mean_s * 1e9:8.1f} ns  "
            f"wait {st.wait_mean_s * 1e9:7.1f} ns  {st.energy_pj / max(st.n, 1):9.1f} pJ/call  "
            f"{st.confidence.level.value if st.confidence else '-'} {st.levels}  ws {st.ws_max} {st.regime}  "
            f"code {st.code_bytes} B"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A micro-scheduler on the calibrated A53.")
    ap.add_argument("--jobs", type=int, default=200)
    ap.add_argument("--accels", type=int, default=2)
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--trace", type=Path, help="write the scheduler trace (JSON lines) here"
    )
    args = ap.parse_args(argv)
    res = run(args.jobs, args.accels, n_cores=args.cores, seed=args.seed)
    print_report(res)
    if args.trace:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        args.trace.write_text("".join(json.dumps(t) + "\n" for t in res.trace))
        print(f"trace: {len(res.trace)} operations -> {args.trace}")
    return 0 if res.all_correct else 1


if __name__ == "__main__":
    raise SystemExit(main())
