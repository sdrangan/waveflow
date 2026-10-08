"""AC3 of ``plans/cpu_model.md``: bus traffic is timed by the bus model, never by the CPU model.

A host runs ``f`` on the processor, issues a bus write through its ``MMIFMaster``, then runs ``g``.
The task is split at the bus interaction, as ``Processor``'s docstring prescribes, so the core is not
held during the transfer.  Run once with the write and once without: the CPU's charges are identical,
and the only difference in completion time is the bus transfer, charged once.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from waveflow.cpu import CpuConfig, Processor, SwFunction
from waveflow.hw.aximm import DirectMMIF, MMIFMaster, MMIFSlave
from waveflow.hw.clock import Clock
from waveflow.hw.interface import Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

BUS_HZ = 100e6


@dataclass
class _MemBank(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self._mem: dict[int, int] = {}
        self.slave = MMIFSlave(
            sim=self.sim,
            bitwidth=32,
            rx_write_proc=self.on_write,
            rx_read_proc=self.on_read,
        )

    def on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        for i, w in enumerate(words):
            self._mem[local_addr + i] = int(w)
        yield self.timeout(len(words) / BUS_HZ)  # one bus cycle per word

    def on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.timeout(0)
        return np.array(
            [self._mem.get(local_addr + i, 0) for i in range(nwords)], dtype=np.uint32
        )


def _run(with_bus: bool):
    sim = Simulation()
    clk = Clock(freq=BUS_HZ)
    cpu = Processor(
        name="cpu", sim=sim, config=CpuConfig(f_clk_hz=1.2e9, switch_cycles=50)
    )
    mem = _MemBank(name="mem", sim=sim)
    master = MMIFMaster(sim=sim, bitwidth=32)
    # DirectMMIF charges only its own latency; the per-word time is the slave's (above).
    link = DirectMMIF(sim=sim, clk=clk, byte_addressable=False, latency_write=20)
    link.bind("master", master)
    link.bind("slave", mem.slave)
    f = SwFunction(name="f", fn=lambda: (None, {}), cycles=1200)
    g = SwFunction(name="g", fn=lambda: (None, {}), cycles=2400)
    out = {}

    def host():
        yield from cpu.execute(f)
        out["t_bus0"] = sim.env.now
        if with_bus:
            yield from master.write(np.arange(64, dtype=np.uint32), 0)
        out["t_bus1"] = sim.env.now
        yield from cpu.execute(g)
        out["t_end"] = sim.env.now

    sim.env.process(host())
    sim.env.run()
    out["records"] = [
        (r.name, r.cycles, r.switch_cycles, r.busy_s) for r in cpu.records
    ]
    out["mem"] = dict(mem._mem)
    return out


def test_bus_time_is_charged_once_and_cpu_cycles_do_not_change():
    quiet, busy = _run(False), _run(True)
    assert quiet["records"] == busy["records"]
    bus_s = busy["t_bus1"] - busy["t_bus0"]
    assert bus_s > 0, "the bus model must charge the transfer"
    assert quiet["t_bus1"] == quiet["t_bus0"]
    assert busy["t_end"] - quiet["t_end"] == pytest.approx(bus_s, rel=1e-12)
    assert bus_s == pytest.approx((20 + 64) / BUS_HZ, rel=1e-12)
    assert busy["mem"][63] == 63


def test_the_core_is_free_while_the_host_waits_on_the_bus():
    out = _run(True)
    (_, _, _, busy_f), (_, _, _, busy_g) = out["records"][0], out["records"][1]
    assert busy_f + busy_g < out["t_end"]
