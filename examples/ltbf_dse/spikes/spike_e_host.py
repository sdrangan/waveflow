"""Spike 1.2 e (plans/ltbf_dse/ltbf_dse.md): the host -- a ``SwHost`` whose threads charge a ``Processor``.

The system's software is a :class:`~waveflow.sw.SwHost` (the bus master, the interrupt inputs, the C++
twin for XSI) whose threads run their computation on a :class:`~waveflow.cpu.Processor` (the A53's
cores, its calibrated cycle models).  In pysim a thread does ``yield from host.charge(func, ...)``;
under XSI the C++ twin calls ``compute(n)`` with ``n`` the same call's cycles in host-clock cycles.
For the two to charge the same time, pysim rounds each call's A53 time **up to a host-clock
cycle**, exactly as ``compute(ceil(c * f_host / f_cpu))`` does in C++.

Checked here, in pysim:

* a thread's charged call takes ``ceil(c * f_host / f_cpu) / f_host`` seconds;
* two threads on one core serialize (the second waits for the first), and an ``SwEvent`` set after a
  charged call wakes its waiter at that instant;
* the Processor's records carry each call (name, cycles).

    python -m examples.ltbf_dse.spikes.spike_e_host
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.cpu.platform import CpuPlatform
from waveflow.cpu.processor import Processor
from waveflow.cpu.task import SwFunction
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwEvent, SwHost

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "spikes"
F_HOST = 250e6


def host_cycles(cpu_cycles: float, f_cpu: float, f_host: float = F_HOST) -> int:
    """The host-clock cycles a call of *cpu_cycles* A53 cycles is charged: what the C++ twin passes
    to ``compute(n)``."""
    return math.ceil(cpu_cycles * f_host / f_cpu - 1e-9)


@dataclass
class ChargingHost(SwHost):
    """A host whose threads charge a :class:`Processor`."""

    cpu: Processor | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=F_HOST))
    log: list = field(default_factory=list)

    def charge(self, func: SwFunction, *args: Any, prio: int = 0, **kw: Any):
        """Run *func* on the processor, then pad its time on the core to a whole number of host
        cycles (as ``compute(n)`` charges it).  Queueing for a core is not padded: under XSI it is the
        twin's core semaphore, at host-cycle granularity already."""
        result = yield from self.cpu.execute(func, *args, prio=prio, **kw)
        rec = self.cpu.records[-1]
        n = host_cycles(
            rec.cycles + rec.switch_cycles, self.cpu.config.f_clk_hz, self.clk.freq
        )
        pad = rec.t_start + n * self.clk.period - self.env.now
        if pad > 0:
            yield self.env.timeout(pad)
        return result

    def main(self):
        self.ready = SwEvent(self, name="ready")
        self.start(self._second)
        for i, c in enumerate((1200.0, 1201.0)):
            t0 = self.env.now
            yield from self.charge(SwFunction.fixed(f"first{i}", c))
            self.log.append(("first", i, t0, self.env.now))
        self.ready.set()

    def _second(self):
        t0 = self.env.now
        yield from self.charge(SwFunction.fixed("second", 600.0))
        self.log.append(("second", 0, t0, self.env.now))
        t1 = self.env.now
        yield from self.ready.wait()
        self.log.append(("woken", 0, t1, self.env.now))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    platform = CpuPlatform.load()
    sim = Simulation()
    cpu = Processor(name="cpu", sim=sim, config=platform.cpu_config(n_cores=1))
    host = ChargingHost(name="host", sim=sim, cpu=cpu)
    sim.run_sim()
    f_cpu = cpu.config.f_clk_hz
    tick = 1.0 / F_HOST
    out = {
        "f_cpu_hz": f_cpu,
        "switch_cycles": [r.switch_cycles for r in cpu.records],
        "records": [(r.name, r.cycles) for r in cpu.records],
        "log_ns": [
            (a, i, round(t0 * 1e9, 3), round(t1 * 1e9, 3)) for a, i, t0, t1 in host.log
        ],
    }
    # Each call holds its core for a whole number of host cycles, its A53 time rounded up, and ends
    # on a host-clock edge.
    ends = {who + str(i): t1 for who, i, _t0, t1 in host.log}
    for rec in cpu.records:
        held = (ends[rec.name.replace("second", "second0")] - rec.t_start) / tick
        out.setdefault("held_host_cycles", []).append(round(held, 6))
        out.setdefault("expected", []).append(
            host_cycles(rec.cycles + rec.switch_cycles, f_cpu)
        )
    (OUT / "spike_e.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
