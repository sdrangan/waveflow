"""stream_kernel_mm.py — the free-running poly behind AXI-MM, driven by a software host
(``plans/unit_harness.md`` Stage 2).

The **same kernel** as ``stream_kernel.py`` -- the same class, the same csynth'd RTL -- placed behind the
queue views its type declares (``PolyKernel.mm_views``: a queue in front of ``s_in``, a queue behind
``m_out`` and ``m_resp``), on an AXI-MM crossbar, and driven by a host of **two threads**:

* a **writer** that sends each command's header and samples into queue in, each one packet;
* a **reader** that takes each command's results from queue out and its response from the response
  queue, and checks the response.

Nothing polls: the endpoints sleep on the queues' interrupts.  The host knows how many results each
command produces -- one per sample it sent, so fewer when it ends a burst early -- so the reader takes
exactly those words before the response, and the results queue never fills behind an unread response.

The system is built the D12 way: modules on one simulation, then ``HwSystem(sim, dut)`` naming the DUT
(the device and the crossbar); the host is the testbench, derived.  The host's C++ twin is
``poly_kernel_host.h`` beside this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.mm_device import build_mm_device
from waveflow.hw.mm_host import BoundMemSlaveAdaptor
from waveflow.simulation.simulation import Simulation
from waveflow.sw import SwHost

from examples.stream_kernel.stream_kernel import (
    W,
    Float32,
    PolyKernel,
    Resp,
    default_cmds,
    expected,
    stimulus,
)

#: Where the device's views start on the bus.
MM_BASE = 0x0000
#: The host's bus master keeps one read and one write in flight (what its C++ twin does).
HOST_MAX_OUTSTANDING = 1
HOST_ISSUE_CYCLES = 2
#: The traced host endpoints -- what both hosts dump and the conformance gate compares.
HOST_ENDPOINTS = ("q_in", "q_out", "q_resp")


def _u64(words) -> np.ndarray:
    return np.asarray(words, dtype=np.uint64)


@dataclass
class PolyHost(SwHost):
    """The host program: send every command, collect every result and response.

    Endpoints (set by :func:`mm_system`): ``q_in`` -- a ``StreamIFMaster``, one ``write`` one packet;
    ``q_out`` and ``q_resp`` -- unframed ``StreamIFSlave``s, so reads name their size.  The scenario is
    one burst per command, ``[k, nresp, nhdr, <header words>, <sample words>]``: ``k`` results to take,
    a response of ``nresp`` words.  :meth:`write_scenario` writes it; given :attr:`scenario`, this host
    runs from that file, as its C++ twin (``PolyHostModel``, ``poly_kernel_host.h``) does."""

    cmds: list = field(default_factory=default_cmds)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Cycles between polls, should an endpoint poll (none does: they wait on interrupts).
    poll_cycles: int = 8

    cpp_model: ClassVar[str | None] = "PolyHostModel"
    cpp_header: ClassVar[str | None] = "poly_kernel_host.h"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=W, max_outstanding=HOST_MAX_OUTSTANDING,
                            issue_cycles=HOST_ISSUE_CYCLES)
        self.q_in = self.q_out = self.q_resp = None
        self.irq: dict[str, IrqIFSink] = {v: self.add_irq(f"irq_{v}") for v in HOST_ENDPOINTS}
        self.done = self.env.event()
        #: Per command: the results words and the response, as read.
        self.results: list[np.ndarray] = []
        self.resps: list[np.ndarray] = []

    def scenario_bursts(self) -> list[np.ndarray]:
        out = []
        for c in self.cmds:
            hdr, samples = stimulus([c])
            nresp = Resp.nwords_per_inst(W)
            out.append(np.concatenate([_u64([c.n_sent, nresp, len(hdr.words)]),
                                       _u64(hdr.words), _u64(samples.words)]))
        return out

    def pre_sim(self) -> None:
        super().pre_sim()
        if self.scenario:
            from waveflow.utils.burst_io import read_burst_bundle
            bursts = read_burst_bundle(self.scenario)
        else:
            bursts = self.scenario_bursts()
        #: ``(k, nresp, header words, sample words)`` per command.
        self.items: list[tuple] = []
        for b in bursts:
            b = _u64(b)
            k, nresp, nhdr = (int(v) for v in b[:3])
            self.items.append((k, nresp, b[3:3 + nhdr], b[3 + nhdr:]))

    def _writer(self):
        for _k, _nresp, hdr, samples in self.items:
            yield from self.q_in.write(hdr)
            yield from self.q_in.write(samples)

    def _reader(self):
        for k, nresp, _hdr, _samples in self.items:
            y = yield from self.q_out.get(nwords_max=k)
            self.results.append(_u64(y))
            r = yield from self.q_resp.get(nwords_max=nresp)
            self.resps.append(_u64(r))
        self.done.succeed()

    def bfm_model(self):
        if int(self.m.max_outstanding) != 1:
            raise ValueError(f"PolyHostModel's AxiMmMaster keeps one read and one write in flight; "
                             f"{self.name}.m.max_outstanding is {self.m.max_outstanding}")
        return super().bfm_model()

    def main(self):
        """Two threads: the writer, started here, and the reader, which is this thread."""
        self.start(self._writer)
        yield from self._reader()


#: A direct link's latencies (``direct=True``): the host's AXI4 group wired straight into the device's
#: front -- ``assign``s in the system top, so **zero**: a wire has no registers, and the front's own
#: are in the adaptor's model.  Physics, not a fit: 2 cycles gave +6.1% against the RTL, 0 gives +3.2%.
DIRECT_LATENCY_INIT, DIRECT_LATENCY_TRAVEL = 0.0, 0.0


def mm_system(cmds=None, direct: bool = False) -> HwSystem:
    """The free-running poly behind AXI-MM with its host: modules on one simulation, then
    ``HwSystem(sim, dut)`` (D12) -- the DUT is the device and the crossbar, the host the testbench.

    ``direct=True`` is the **device's own harness** (``plans/unit_harness.md`` D9): its views behind
    one front, the host bound straight to that front's slave group by a direct link -- no crossbar --
    so what is measured is the device's own timing."""
    cmds = list(default_cmds() if cmds is None else cmds)
    sim = Simulation()
    clk = Clock(freq=100e6)
    kernel = PolyKernel(name=PolyKernel.cpp_kernel_name, sim=sim, clk=clk)
    host = PolyHost(name="host", sim=sim, cmds=cmds, clk=clk)
    device = build_mm_device(kernel, sim=sim, clk=clk, mem_dwidth=W, one_front=direct)
    slaves, ranges = device.ranges(MM_BASE)
    lat = (DIRECT_LATENCY_INIT, DIRECT_LATENCY_TRAVEL) if direct else (4.0, 2.0)
    xbar = AXIMMCrossBarIF(name="link" if direct else "xbar", sim=sim, clk=clk, nports_master=1,
                           nports_slave=len(slaves), bitwidth=W, latency_init=lat[0],
                           latency_travel=lat[1])
    xbar.direct = direct
    xbar.bind("master_0", host.m)
    for k, ep in enumerate(slaves):
        xbar.bind(f"slave_{k}", ep)
    assign_address_ranges(slaves, ranges)
    mm = BoundMemSlaveAdaptor(device.layout.at(MM_BASE), host.m, poll_cycles=host.poll_cycles)
    for name in HOST_ENDPOINTS:
        line = IrqIF(name=f"{name}_irq", sim=sim)
        line.bind("source", device.views[name].m_irq)
        line.bind("sink", host.irq[name])
    host.q_in = mm.stream_master("q_in", irq=host.irq["q_in"])
    host.q_out = mm.stream_slave("q_out", irq=host.irq["q_out"])
    host.q_resp = mm.stream_slave("q_resp", irq=host.irq["q_resp"])
    dut = DutTop.of("poly_dev_dut" if direct else "poly_mm_dut", sim, comps=[device], ifs=[xbar])

    def check(sysm):
        want_y, want_r = expected(cmds)
        return {"results": host.results, "resps": host.resps,
                "want_results": want_y, "want_resps": want_r}

    sysm = HwSystem(sim, dut, run_until=host.done, check=check,
                    fresh=lambda: mm_system(cmds, direct=direct))
    sysm.host, sysm.device, sysm.xbar = host, device, xbar
    return sysm


__all__ = ["PolyHost", "mm_system"]
