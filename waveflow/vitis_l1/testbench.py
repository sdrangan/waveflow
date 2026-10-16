"""testbench.py — the ``VitisFft`` testbench graph, its scenario files and its golden.

``R`` stream drivers -> :class:`~waveflow.vitis_l1.hw.VitisFft` -> ``R`` sinks, as a graph: the same
object runs the pysim and, through ``tb_top_spec``, generates the XSI harness that drives the RTL.
Both read their stimulus from the same burst bundles, so both play the same frames with the same gaps,
and both are checked against the same golden (the bit-exact model in :mod:`waveflow.vitis_l1.fft`).

Framework, not example code: the calibration fixture (``waveflow/calib/fixtures/vitis_fft.py``) drives
the module through it, and ``examples/vitis_fft`` is a worked use of it.
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
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle
from waveflow.vitis_l1 import fft as fft_model
from waveflow.vitis_l1.hw import VitisFft, _pack_complex, _unpack_complex
from waveflow.vitis_l1.timing import PLATFORM

R = 4
#: The default width configuration -- the one the vendor goldens pin and the shipped calibration
#: covers.  Every helper takes the four as keywords, so another configuration is a call, not a fork.
IN_W, IN_I = 16, 2
TW_W, TW_I = 18, 2
DEFAULT_CONFIG = {"in_w": IN_W, "in_i": IN_I, "tw_w": TW_W, "tw_i": TW_I}
#: The platform clock: the RFSoC 4x2 fabric at 250 MHz (``RFSOC4X2_PERIOD_NS`` = 4 ns).
CLK_HZ = 250e6


def default_platform_dir() -> Path | None:
    """The packaged calibration platform ``VitisFft`` is measured on, or ``None`` if unavailable."""
    from waveflow.calib.platform import packaged_platforms_dir
    root = packaged_platforms_dir()
    d = root / PLATFORM if root is not None else None
    return d if d is not None and d.is_dir() else None


@dataclass
class TimedSink(StreamSink):
    """A :class:`StreamSink` that also records **when** each burst finished arriving.

    The XSI ``AxisSlave`` timestamps every word; this is the pysim's equivalent at burst granularity,
    which is all the module's timing model resolves (one event per frame per lane).
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stamps: list[float] = []

    def rx_proc(self, words) -> ProcessGen[None]:
        self.stamps.append(self.now)
        yield from super().rx_proc(words)


@dataclass
class VitisFftTB(FreeRunMod):
    """The testbench as a graph: ``R`` drivers -> :class:`VitisFft` -> ``R`` sinks.

    Lane ``j`` carries samples ``j, j+R, j+2R, ...`` of every frame, each frame one burst of ``L/R``
    words per lane.  Timing, in precedence: ``untimed``; explicit ``proc_cycles``/``ii_cycles``; the
    calibrated ``platform_dir`` (by default the packaged :data:`~waveflow.vitis_l1.timing.PLATFORM`).
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    length: int = 16
    n_frames: int = 4
    in_w: int = IN_W
    in_i: int = IN_I
    tw_w: int = TW_W
    tw_i: int = TW_I
    #: Cycles the XSI main runs for: comfortably past the last frame.
    n_cycles: int = 2000
    untimed: bool = False
    proc_cycles: float | None = None
    ii_cycles: float | None = None
    platform_dir: "str | Path | None" = None
    #: Passed to the module: the calibration fixture runs it before the first fit.
    require_calibrated: bool = True
    #: Idle cycles between frames on every input lane (both backends).  0 = back to back.
    burst_gap_cycles: int = 0
    #: Per-frame gaps (entry k: before frame k + 1), overriding ``burst_gap_cycles`` while they last.
    burst_gaps: list = field(default_factory=list)
    #: XSI only: have each driver dump its accepted words with their cycles (``vectors/s_in_<j>_acc``),
    #: so the RTL is timed at its ports from the BFMs, with no waveform.
    capture_accepts: bool = False
    root: Path | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        timing: dict = {}
        if not self.untimed:
            if self.proc_cycles is not None or self.ii_cycles is not None:
                timing = {"proc_cycles": self.proc_cycles, "ii_cycles": self.ii_cycles}
            else:
                pdir = self.platform_dir if self.platform_dir is not None else default_platform_dir()
                if pdir is not None:
                    timing = {"platform_dir": pdir, "require_calibrated": self.require_calibrated}
        self.dut = VitisFft(name="vitis_fft", sim=self.sim, clk=self.clk, L=self.length,
                            in_w=self.in_w, in_i=self.in_i, tw_w=self.tw_w, tw_i=self.tw_i,
                            **timing)
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


# -- the scenario: written once, read by both backends -------------------------------------------
    @property
    def config(self) -> dict:
        return {"in_w": self.in_w, "in_i": self.in_i, "tw_w": self.tw_w, "tw_i": self.tw_i}


def input_frames(n_frames: int, length: int, seed: int = 0, *, in_w: int = IN_W, **_):
    """``n_frames`` random full-scale complex frames, as stored ``ap_fixed<in_w, in_i>`` integers."""
    rng = np.random.default_rng(seed)
    lim = 1 << (in_w - 1)
    return [(rng.integers(-lim, lim, length), rng.integers(-lim, lim, length))
            for _ in range(n_frames)]


def write_scenario(root, n_frames: int, length: int, seed: int = 0, *, in_w: int = IN_W,
                   **_) -> None:
    """One bundle per input lane under ``<root>/vectors``, one burst per frame."""
    for j in range(R):
        bursts = [_pack_complex(x_re[j::R], x_im[j::R], in_w)
                  for x_re, x_im in input_frames(n_frames, length, seed, in_w=in_w)]
        write_burst_bundle(bursts, Path(root) / "vectors" / f"s_in_{j}")


def golden(n_frames: int, length: int, seed: int = 0, *, in_w: int = IN_W, in_i: int = IN_I,
           tw_w: int = TW_W, tw_i: int = TW_I):
    """The expected output of every frame, ``[(re, im), ...]``, from the bit-exact model."""
    out = []
    for x_re, x_im in input_frames(n_frames, length, seed, in_w=in_w):
        if length == 16:
            y_re, y_im, _ = fft_model.fft16(x_re, x_im, in_w, in_i, tw_w, tw_i)
        else:
            y_re, y_im, _ = fft_model.fft_general(x_re, x_im, length, in_w, in_i, tw_w, tw_i)
        out.append((y_re, y_im))
    return out


def frames_from_lanes(lanes: list[np.ndarray], out_w: int, length: int):
    """Reassemble per-lane flat word arrays (every frame back to back) into ``[(re, im), ...]``."""
    per_lane = length // R
    n = len(lanes[0]) // per_lane
    out = []
    for k in range(n):
        y_re = np.zeros(length, dtype=np.int64)
        y_im = np.zeros(length, dtype=np.int64)
        for j in range(R):
            y_re[j::R], y_im[j::R] = _unpack_complex(lanes[j][k * per_lane:(k + 1) * per_lane],
                                                     out_w)
        out.append((y_re, y_im))
    return out


def run_pysim(root, **kw) -> VitisFftTB:
    """Write the scenario under ``<root>/vectors`` and run the testbench in SimPy."""
    tb = VitisFftTB(name="tb", sim=Simulation(), root=Path(root), **kw)
    write_scenario(root, tb.n_frames, tb.length, **tb.config)
    tb.sim.run_sim()
    return tb


def pysim_frame_cycles(tb: VitisFftTB) -> list[float]:
    """When each output frame finished arriving (all lanes), in cycles from the start."""
    per_lane = [s.stamps for s in tb.sinks]
    return [max(lane[k] for lane in per_lane) / tb.clk.period
            for k in range(len(per_lane[0]))]


def pysim_output(tb: VitisFftTB):
    lanes = [np.concatenate(s.words) for s in tb.sinks]
    return frames_from_lanes(lanes, int(tb.dut.out_fmt.W), tb.length)
