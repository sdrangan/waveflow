"""The ``SsrFft`` testbench graph: ``R`` drivers -> :class:`~.hw.SsrFft` -> ``R`` sinks.

The same graph as ``VitisFft``'s (:mod:`waveflow.vitis_l1.testbench`), with the other DUT: the two
modules share a port group, so they share the scenario files, the lane packing and the golden --
which is the point.  One object runs the pysim and, through ``tb_top_spec``, generates the XSI
harness that drives the RTL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver
from waveflow.vitis_l1.testbench import (CLK_HZ, IN_I, IN_W, TW_I, TW_W, TimedSink,
                                         frames_from_lanes, golden, write_scenario)

from .hw import SsrFft

R = 4

__all__ = ["SsrFftTB", "golden", "write_scenario", "run_pysim", "pysim_output",
           "pysim_frame_cycles"]


@dataclass
class SsrFftTB(FreeRunMod):
    """``R`` drivers -> :class:`SsrFft` -> ``R`` sinks; lane ``j`` carries samples ``j, j+R, ...``."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    length: int = 64
    n_frames: int = 4
    in_w: int = IN_W
    in_i: int = IN_I
    tw_w: int = TW_W
    tw_i: int = TW_I
    n_cycles: int = 4000
    timed: bool = True
    reorder: str = "sob"
    burst_gap_cycles: int = 0
    burst_gaps: list = field(default_factory=list)
    capture_accepts: bool = False
    root: Path | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = SsrFft(name="ssr_fft", sim=self.sim, clk=self.clk, L=self.length,
                          in_w=self.in_w, in_i=self.in_i, tw_w=self.tw_w, tw_i=self.tw_i,
                          timed=self.timed, reorder=self.reorder)
        in_bw, out_bw = 2 * int(self.in_w), 2 * int(self.dut.out_fmt.W)
        per_lane = self.length // R
        self.drivers = [StreamDriver(name=f"drv_{j}", sim=self.sim, bitwidth=in_bw,
                                     has_tlast=True, in_bundle=f"vectors/s_in_{j}", root=self.root,
                                     burst_gap_cycles=int(self.burst_gap_cycles),
                                     burst_gaps=[int(g) for g in self.burst_gaps],
                                     accept_bundle=(f"vectors/s_in_{j}_acc"
                                                    if self.capture_accepts else ""))
                        for j in range(R)]
        self.sinks = [TimedSink(name=f"snk_{j}", sim=self.sim, bitwidth=out_bw, has_tlast=True,
                                out_bundle=f"vectors/m_out_{j}",
                                queue_size=max(64, per_lane * self.n_frames))
                      for j in range(R)]
        for c in (self.dut, *self.drivers, *self.sinks):
            self.add_comp(c)
        for j in range(R):
            i = StreamIF(name=f"in_if_{j}", sim=self.sim, clk=self.clk, bitwidth=in_bw)
            i.bind(ep_name="master", endpoint=self.drivers[j].stream_ep)
            i.bind(ep_name="slave", endpoint=self.dut.s_in[j])
            self.add_if(i)
            o = StreamIF(name=f"out_if_{j}", sim=self.sim, clk=self.clk, bitwidth=out_bw)
            o.bind(ep_name="master", endpoint=self.dut.m_out[j])
            o.bind(ep_name="slave", endpoint=self.sinks[j].stream_ep)
            self.add_if(o)
        self.boundary = []

    @property
    def config(self) -> dict:
        return {"in_w": self.in_w, "in_i": self.in_i, "tw_w": self.tw_w, "tw_i": self.tw_i}


def run_pysim(root, **kw) -> SsrFftTB:
    """Write the scenario under ``<root>/vectors`` and run the testbench in SimPy."""
    tb = SsrFftTB(name="tb", sim=Simulation(), root=Path(root), **kw)
    write_scenario(root, tb.n_frames, tb.length, **tb.config)
    tb.sim.run_sim()
    return tb


def pysim_frame_cycles(tb: SsrFftTB) -> list[float]:
    """When each output frame finished arriving (all lanes), in cycles from the start."""
    per_lane = [s.stamps for s in tb.sinks]
    return [max(lane[k] for lane in per_lane) / tb.clk.period for k in range(len(per_lane[0]))]


def pysim_output(tb: SsrFftTB):
    lanes = [np.concatenate(s.words) for s in tb.sinks]
    return frames_from_lanes(lanes, int(tb.dut.out_fmt.W), tb.length)
