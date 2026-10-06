"""The ``SsrFft`` testbench graph: driver(s) -> :class:`~.hw.SsrFft` -> sink(s).

One object runs the pysim and, through ``tb_top_spec``, generates the XSI harness that drives the
RTL; both read the same scenario files and are checked against the same golden (the bit-exact vendor
model, :mod:`waveflow.vitis_l1.fft`, on the frames :func:`waveflow.vitis_l1.testbench.input_frames`
draws -- the same frames ``VitisFft``'s testbench uses).

Two boundaries, matching the module's:

* **``lanes=False``** (the module's default): one driver and one sink on the ``RadixWord`` ports, a
  frame one burst of ``L/R`` words, each word ``R`` complex samples -- 128 bits in at 16-bit input,
  which the XSI BFMs carry as ``k = ceil(W/64)`` 64-bit chunks;
* **``lanes=True``**: ``VitisFft``'s graph -- ``R`` drivers and ``R`` sinks, lane ``j`` carrying samples
  ``j, j+R, ...`` -- so the two modules share scenario files too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver
from waveflow.utils import complexutils as cx
from waveflow.utils.burst_io import read_burst_bundle, write_burst_bundle
from waveflow.vitis_l1.testbench import (CLK_HZ, IN_I, IN_W, TW_I, TW_W, TimedSink,
                                         frames_from_lanes, golden, input_frames)
from waveflow.vitis_l1.testbench import write_scenario as write_lane_scenario

from .hw import SsrFft
from .model import Geometry, to_words
from .types import EdgeType

R = 4

__all__ = ["SsrFftTB", "golden", "write_scenario", "run_pysim", "pysim_output",
           "pysim_frame_cycles", "frames_from_words", "port_names"]


def _edges(length: int, in_w: int = IN_W, in_i: int = IN_I, tw_w: int = TW_W, tw_i: int = TW_I,
           **_) -> tuple[EdgeType, EdgeType]:
    geo = Geometry(length, in_w, in_i, tw_w, tw_i)
    return EdgeType("in", geo.in_fmt, geo.R), EdgeType("out", geo.out_fmt, geo.R)


def _chunks(bitwidth: int) -> int:
    return -(-int(bitwidth) // 64)


def port_names(lanes: bool) -> tuple[list[str], list[str]]:
    """The boundary ports' names: ``(["s_in"], ["m_out"])``, or the ``R`` lanes of each."""
    if lanes:
        return [f"s_in_{j}" for j in range(R)], [f"m_out_{j}" for j in range(R)]
    return ["s_in"], ["m_out"]


def write_scenario(root, n_frames: int, length: int, seed: int = 0, *, lanes: bool = False,
                   **cfg) -> None:
    """The input bundles under ``<root>/vectors``, one burst per frame.

    With ``lanes``, ``VitisFft``'s four lane bundles.  Without, one ``vectors/s_in`` bundle of
    ``RadixWord`` s -- packed by the schema, ``k`` 64-bit chunks a word."""
    if lanes:
        write_lane_scenario(root, n_frames, length, seed, **cfg)
        return
    e_in, _ = _edges(length, **cfg)
    bursts = []
    for x_re, x_im in input_frames(n_frames, length, seed, in_w=cfg.get("in_w", IN_W)):
        re, im = to_words(np.asarray(x_re)), to_words(np.asarray(x_im))
        words = write_array(cx.make_complex(re.reshape(-1), im.reshape(-1), e_in.fmt),
                            elem_type=e_in.elem, word_bw=e_in.bitwidth)
        bursts.append(np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e_in.bitwidth)))
    write_burst_bundle(bursts, Path(root) / "vectors" / "s_in",
                       word_chunks=_chunks(e_in.bitwidth))


def frames_from_words(words: np.ndarray, length: int, **cfg) -> list:
    """``RadixWord`` output words (``(n, k)`` chunks, every frame back to back) -> ``[(re, im), ...]``."""
    _, e_out = _edges(length, **cfg)
    words = np.asarray(words, dtype=np.uint64).reshape(-1, _chunks(e_out.bitwidth))
    v = read_array(words, elem_type=e_out.elem, word_bw=e_out.bitwidth,
                   shape=words.shape[0] * e_out.R).val
    re = np.asarray(v["re"], dtype=np.int64)
    im = np.asarray(v["im"], dtype=np.int64)
    return [(re[k:k + length], im[k:k + length]) for k in range(0, re.size - length + 1, length)]


@dataclass
class SsrFftTB(FreeRunMod):
    """Driver(s) -> :class:`SsrFft` -> sink(s); see the module docstring for the two boundaries."""

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
    lanes: bool = False
    burst_gap_cycles: int = 0
    burst_gaps: list = field(default_factory=list)
    capture_accepts: bool = False
    root: Path | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = SsrFft(name="ssr_fft", sim=self.sim, clk=self.clk, L=self.length,
                          in_w=self.in_w, in_i=self.in_i, tw_w=self.tw_w, tw_i=self.tw_i,
                          timed=self.timed, reorder=self.reorder, lanes=self.lanes)
        ins, outs = port_names(self.lanes)
        d_in = self.dut.s_in if self.lanes else [self.dut.s_in]
        d_out = self.dut.m_out if self.lanes else [self.dut.m_out]
        words_per_frame = self.length // R
        self.drivers = [StreamDriver(name=f"drv_{j}", sim=self.sim, bitwidth=ep.bitwidth,
                                     has_tlast=True, in_bundle=f"vectors/{name}", root=self.root,
                                     burst_gap_cycles=int(self.burst_gap_cycles),
                                     burst_gaps=[int(g) for g in self.burst_gaps],
                                     accept_bundle=(f"vectors/{name}_acc"
                                                    if self.capture_accepts else ""))
                        for j, (name, ep) in enumerate(zip(ins, d_in))]
        self.sinks = [TimedSink(name=f"snk_{j}", sim=self.sim, bitwidth=ep.bitwidth, has_tlast=True,
                                out_bundle=f"vectors/{name}",
                                queue_size=max(64, words_per_frame * self.n_frames))
                      for j, (name, ep) in enumerate(zip(outs, d_out))]
        for c in (self.dut, *self.drivers, *self.sinks):
            self.add_comp(c)
        for j, (drv, ep) in enumerate(zip(self.drivers, d_in)):
            i = StreamIF(name=f"in_if_{j}", sim=self.sim, clk=self.clk, bitwidth=ep.bitwidth)
            i.bind(ep_name="master", endpoint=drv.stream_ep)
            i.bind(ep_name="slave", endpoint=ep)
            self.add_if(i)
        for j, (snk, ep) in enumerate(zip(self.sinks, d_out)):
            o = StreamIF(name=f"out_if_{j}", sim=self.sim, clk=self.clk, bitwidth=ep.bitwidth)
            o.bind(ep_name="master", endpoint=ep)
            o.bind(ep_name="slave", endpoint=snk.stream_ep)
            self.add_if(o)
        self.boundary = []

    @property
    def config(self) -> dict:
        return {"in_w": self.in_w, "in_i": self.in_i, "tw_w": self.tw_w, "tw_i": self.tw_i}


def run_pysim(root, **kw) -> SsrFftTB:
    """Write the scenario under ``<root>/vectors`` and run the testbench in SimPy."""
    tb = SsrFftTB(name="tb", sim=Simulation(), root=Path(root), **kw)
    write_scenario(root, tb.n_frames, tb.length, lanes=tb.lanes, **tb.config)
    tb.sim.run_sim()
    return tb


def pysim_frame_cycles(tb: SsrFftTB) -> list[float]:
    """When each output frame finished arriving (on every output port), in cycles from the start."""
    per_port = [s.stamps for s in tb.sinks]
    return [max(p[k] for p in per_port) / tb.clk.period for k in range(len(per_port[0]))]


def pysim_output(tb: SsrFftTB):
    if tb.lanes:
        lanes = [np.concatenate(s.words) for s in tb.sinks]
        return frames_from_lanes(lanes, int(tb.dut.out_fmt.W), tb.length)
    return frames_from_words(np.concatenate(tb.sinks[0].words), tb.length, **tb.config)


def captured_frames(xsi_dir: Path, length: int, lanes: bool, **cfg):
    """What the RTL's sink(s) captured (``vectors/<port>``), as ``[(re, im), ...]``."""
    vec = Path(xsi_dir) / "vectors"
    if lanes:
        _, e_out = _edges(length, **cfg)
        out_w = e_out.fmt.W
        lanes_w = [np.concatenate(read_burst_bundle(vec / f"m_out_{j}")) for j in range(R)]
        return frames_from_lanes(lanes_w, out_w, length)
    return frames_from_words(np.concatenate(read_burst_bundle(vec / "m_out")), length, **cfg)
