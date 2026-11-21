"""The offload on the bus: the device, a memory and a host on one crossbar, as ``HwSystem(sim, dut)``.

The host is the testbench; the DUT is the device, the memory and the crossbar.  The host:

* a **writer** thread: per job, waits for a free slot, writes the input buffer into memory over the
  bus, then sends the command into ``q_cmd`` (its endpoint sleeps on the queue's room interrupt);
* a **reader** thread: per job, takes a status from ``q_status`` (its endpoint sleeps on the queue's
  data interrupt -- never a poll), then reads that job's output back over the bus and frees the slot.

A status means the output is in memory (the writer echoes it after the write), so the reader reads the
output only after its status.  At most :data:`MAX_IN_FLIGHT` jobs are outstanding, which bounds what the
status queue must hold.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.arrayutils import array, read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, BusReader
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost, SwSemaphore

from scale_dev import STATUS_DEPTH, Job, ScaleDev, scenario
from scale_msgs import DW, I16, NMAX, Code, ScaleStatus, nwords, scale

#: The device's views, and the memory, on the bus (byte addresses).
DEV_BASE = 0x0000
MEM_BASE = 0x10_0000
MEM_WORDS = 8192
#: Jobs the host keeps outstanding: at most the status queue's depth (one status word a job).
MAX_IN_FLIGHT = 4
assert MAX_IN_FLIGHT <= STATUS_DEPTH


@dataclass
class ScaleHost(SwHost):
    jobs: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=200e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=DW)
        self.irq: dict[str, IrqIFSink] = {v: self.add_irq(f"irq_{v}") for v in ("q_cmd", "q_status")}
        self.q_cmd = self.q_status = None              # set by the wiring
        self.bus = BusReader(self.m)
        self.done = self.env.event()
        self.status: list[ScaleStatus] = []
        self.out: dict[int, np.ndarray] = {}
        self.slots = SwSemaphore(self, MAX_IN_FLIGHT, name="slots")

    def scenario_bursts(self) -> list[np.ndarray]:
        """What the host's run is made of, one burst per job: ``[the command's words | the input
        words]`` -- what a C++ twin of this host would replay at RTL (not written: the RTL step)."""
        out = []
        for j in self.jobs:
            cmd = np.asarray(j.cmd().serialize(word_bw=DW), dtype=np.uint64)
            x = np.asarray(write_array(array(I16, j.x), word_bw=DW), dtype=np.uint64)
            out.append(np.concatenate([cmd, x]))
        return out

    def _byte(self, word_addr: int) -> int:
        return int(word_addr) * (DW // 8)

    def _writer(self):
        for j in self.jobs:
            yield from self.slots.acquire()
            if j.n <= NMAX:
                words = np.asarray(write_array(array(I16, j.x), word_bw=DW), dtype=np.uint64)
                yield from self.bus.write(words, self._byte(j.src))
            yield from self.q_cmd.write(j.cmd())

    def _reader(self):
        for _ in self.jobs:
            st = yield from self.q_status.get_schema(ScaleStatus)
            self.status.append(st)
            if st.code != Code.OK:                    # fatal: the device answers nothing more
                break
            j = self.jobs[int(st.tx_id)]
            words = yield from self.bus.read(nwords(j.n), self._byte(j.dst))
            self.out[j.tx_id] = np.asarray(read_array(words, I16, word_bw=DW, shape=j.n).val)
            self.slots.release()
        self.done.succeed()

    def main(self):
        self.start(self._writer)
        yield from self._reader()


def scale_system(kind: str = "test") -> HwSystem:
    jobs: list[Job] = scenario(kind)
    for j in jobs:                                   # the scenario's word offsets, on the bus
        j.src += MEM_BASE // (DW // 8)
        j.dst += MEM_BASE // (DW // 8)
    sim = Simulation()
    clk = Clock(freq=200e6)
    dev = ScaleDev(name="scale", sim=sim, clk=clk)
    mem = MemoryMod(name="mem", sim=sim, word_size=DW, inline=False, clk=clk, nwords_tot=MEM_WORDS,
                    addr_unit=AddrUnit.byte)
    mem.alloc(MEM_WORDS)
    host = ScaleHost(name="host", sim=sim, jobs=jobs, clk=clk)

    device = build_mm_device(dev, sim=sim, clk=clk, mem_dwidth=DW)
    slaves, ranges = device.ranges(DEV_BASE)
    masters = [host.m, dev.m_rd_mem, dev.m_wr_mem]
    slaves, ranges = slaves + [mem.s_mm], ranges + [(MEM_BASE, MEM_WORDS * DW // 8)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=DW)
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)

    for name in ("q_cmd", "q_status"):              # each queue's interrupt line to the host
        line = IrqIF(name=f"{name}_irq", sim=sim)
        line.bind("source", device.views[name].m_irq)
        line.bind("sink", host.irq[name])
    mm = BoundMemSlaveAdaptor(device.layout.at(DEV_BASE), host.m)
    host.q_cmd = mm.stream_master("q_cmd", irq=host.irq["q_cmd"])
    host.q_status = mm.stream_slave("q_status", irq=host.irq["q_status"])

    dut = DutTop.of("scale_dut", sim, comps=[device, mem], ifs=[xbar])
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: (host.status, host.out),
                    fresh=lambda: scale_system(kind))
    sysm.host, sysm.dev, sysm.mem, sysm.jobs = host, dev, mem, jobs
    return sysm


__all__ = ["ScaleHost", "scale_system", "MAX_IN_FLIGHT"]
