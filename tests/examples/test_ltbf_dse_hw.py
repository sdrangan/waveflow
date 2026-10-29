"""examples/ltbf_dse/hw: the engines' wrappers on the bus, in pysim (plans/ltbf_dse/ltbf_dse.md, Phase 4).

Each test builds the piece of the system it checks -- a software host on a crossbar with Shared Mem,
the wrapper's memory-mapped device and the RespHub -- and runs it as the spec describes (§5): the
host writes jobs into the wrapper's Job FIFO, sleeps on the Resp FIFO's interrupt, and reads the
results back from Shared Mem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from examples.ltbf_dse.hw.records import SRC_SYS, Resp
from examples.ltbf_dse.hw.resp_hub import RespHub
from examples.ltbf_dse.hw.sys_wrap import SysWrap
from examples.ltbf_dse.ltbf_fixed import reg
from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.interface import StreamIF
from waveflow.hw.irq import IrqIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.memory import AddrUnit, MemoryMod
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor, MemSlaveLayout
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost

W = 64  # mm_host writes queue words of at most 64 bits (mm_host.py:427); the bus is 64-bit
SYS_BASE, HUB_BASE = 0x8000_0000, 0x8001_0000


@dataclass
class JobHost(SwHost):
    """Writes each job's descriptor into ``qjob``, then takes one ``Resp`` a job from ``qresp``."""

    bursts: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))
    cpp_model: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W)
        self.irq = {v: self.add_irq(f"irq_{v}") for v in ("qjob", "qresp")}
        self.qjob = self.qresp = None
        self.done = self.env.event()
        self.resps: list[Resp] = []

    def scenario_bursts(self):
        """The scenario both realizations read: one burst a job (its descriptor's words)."""
        return [np.asarray(b, np.uint64) for b in self.bursts]

    def main(self):
        for b in self.bursts:
            yield from self.qjob.write(b)
        for _ in self.bursts:
            r = yield from self.qresp.get_schema(Resp)
            self.resps.append(r)
        self.done.succeed()


def systolic_system(engine_cfg: dict, kind: str = "test"):
    """The systolic wrapper, the RespHub, Shared Mem at bus address 0 and a host, on one crossbar."""
    sim = Simulation()
    clk = Clock(freq=250e6)
    sysw = SysWrap(name="sys", sim=sim, clk=clk, engine_cfg=engine_cfg)
    hub = RespHub(name="hub", sim=sim, clk=clk, n_src=1, word_bits=W)
    work = sysw.engine.workload(kind)
    image = work["m_in"]
    mem = MemoryMod(name="mem", sim=sim, word_size=W, inline=False, clk=clk,
                    nwords_tot=len(image), addr_unit=AddrUnit.byte)  # fmt: skip
    assert mem.alloc(len(image)) == 0  # Shared Mem's one region, at bus address 0
    host = JobHost(
        name="host", sim=sim, clk=clk, bursts=[b.words for b in work["s_job"]]
    )
    sdev = build_mm_device(sysw, sim=sim, clk=clk, mem_dwidth=W, prefix="sys_")
    hdev = build_mm_device(hub, sim=sim, clk=clk, mem_dwidth=W, prefix="hub_")
    link = StreamIF(name="sys_resp", sim=sim, clk=clk, bitwidth=W)
    link.bind("master", sysw.m_resp)
    link.bind("slave", hub.s_in0)
    masters = [host.m, sysw.m_in, sysw.m_out]
    ss, sr = sdev.ranges(SYS_BASE)
    hs, hr = hdev.ranges(HUB_BASE)
    slaves = ss + hs + [mem.s_mm]
    ranges = sr + hr + [(0, len(image) * W // 8)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=W)  # fmt: skip
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, ranges)
    smap = MemSlaveLayout.of(SysWrap, mem_dwidth=W).at(SYS_BASE)
    hmap = MemSlaveLayout.of(RespHub, mem_dwidth=W).at(HUB_BASE)
    for dev, view, key in ((sdev, "qjob", "qjob"), (hdev, "qresp", "qresp")):
        line = IrqIF(name=f"{view}_irq", sim=sim)
        line.bind("source", dev.views[view].m_irq)
        line.bind("sink", host.irq[key])
    host.qjob = BoundMemSlaveAdaptor(smap, host.m).stream_master(
        "qjob", irq=host.irq["qjob"]
    )
    host.qresp = BoundMemSlaveAdaptor(hmap, host.m).stream_slave(
        "qresp", irq=host.irq["qresp"]
    )
    dut = DutTop.of("ltbf_sys_dut", sim, comps=[sdev, hdev, mem], ifs=[xbar, link])
    sysm = HwSystem(sim, dut, run_until=host.done, check=lambda s: host.resps)
    sysm.mem, sysm.host, sysm.sysw, sysm.image = mem, host, sysw, image
    return sysm


def test_systolic_wrapper_runs_jobs_from_its_job_fifo():
    """Step 4.1: jobs written over the bus into the systolic wrapper's ``qjob`` run on the unchanged
    ``SystolicEngineMem``; each is answered by one ``Resp`` (``src`` 0, its ``tx_id``, OK) in the Resp
    FIFO after its ``C`` is in Shared Mem, and every ``C`` equals ``linalg.matmul``'s.
    """
    f = reg(12, 1)
    cfg = {"word_bits": W, "Mmax": 8, "Kmax": 8, "Nmax": 8, "Pmax": 4, "R": 4, "C": 8, "L": 4,
           "a": f, "b": f, "c": f, "pysim": "tiles"}  # fmt: skip
    sysm = systolic_system(cfg)
    mem = sysm.mem
    mem._mem.write(0, sysm.image)  # Shared Mem preloaded with A and B (block-major)
    resps = sysm.run()
    jobs = [d for d, *_ in sysm.sysw.engine._layout("test")[0]]
    assert [int(r.src) for r in resps] == [SRC_SYS] * len(jobs)
    assert sorted(int(r.tx_id) for r in resps) == sorted(int(d.tx_id) for d in jobs)
    assert all(int(r.status) == 0 for r in resps)
    for c_base, want in sysm.sysw.engine.expected_c("test"):
        got = mem._mem.read(c_base * W // 8, len(want))
        assert np.array_equal(np.asarray(got, np.uint64).reshape(want.shape), want)
