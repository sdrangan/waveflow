"""vitis_fft.py — the AMD Vitis L1 SSR FFT as a Waveflow module, frames in and frames out.

``VitisFft`` (:mod:`waveflow.vitis_l1.hw`) is the module: an ``R``-wide AXI-Stream port group each
side, bits from the bit-exact model in :mod:`waveflow.vitis_l1.fft`, and a C++ body that calls the
vendor's ``xf::dsp::fft::fft<>``.  This file is the **testbench** around it, as a graph:

    StreamDriver x R  ->  VitisFft  ->  StreamSink x R

One graph, two backends.  The pysim runs it in SimPy; ``vitis_fft_build.py`` lowers the same graph to
an XSI harness that drives the synthesized RTL.  Both read the stimulus from the same burst bundles
under ``vectors/``, and both are checked against the same golden.

What it demonstrates is the thing a single frame cannot show: **several frames back to back**, so
that the module's latency and its initiation interval (II) are separately visible.  See
``docs/guide/vitis_l1/timing.md``.

    python -m examples.vitis_fft.vitis_fft          # pysim: bits + timing, no toolchain
"""
from __future__ import annotations

import json
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

HERE = Path(__file__).resolve().parent

#: The gated configuration.  L=16 keeps csynth and the XSI run short, and is where the vendor
#: goldens, the cosim measurement and the RTL gate all overlap.
L = 16
R = 4
IN_W, IN_I = 16, 2
TW_W, TW_I = 18, 2
N_FRAMES = 4
#: The RFSoC 4x2 fabric clock the example is synthesized for (``RFSOC4X2_PERIOD_NS`` = 4 ns).
CLK_HZ = 250e6

#: The RTL timing the pysim is configured with, per L: written by
#: ``python -m examples.vitis_fft.vitis_fft_build --measure`` and tracked, because reproducing it needs
#: Vitis and Vivado.  **Measured, not estimated** -- see :func:`measured_timing`.
MEASURED = HERE / "measured" / "vitis_fft_timing.json"


def measured_timing(length: int = L) -> dict | None:
    """The calibrated timing for *length*, or ``None`` if it has not been measured.

    ``ii_cycles`` is the back-to-back frame interval: exact, because a saturated core stays in step
    with its internal commutator.  ``latency_mean`` is the mean residence of an **isolated** frame
    over a sweep of arrival phases; ``latency_min``/``latency_max`` bound it.  The spread is real and
    not reducible by an LT model: the vendor core's input transposer runs a commutator on a
    free-running internal cycle, a frame waits for it by an amount set by its arrival phase, and an
    LT model does not know that phase.  So the pysim uses the mean, and the bound is its stated error.
    """
    if not MEASURED.exists():
        return None
    return json.loads(MEASURED.read_text(encoding="utf-8"))["sizes"].get(str(length))


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

    Walkable, so ``tb_top_spec`` generates the XSI harness from it; runnable, so the pysim golden is
    the same object.  Lane ``j`` carries samples ``j, j+R, j+2R, ...`` of every frame, each frame one
    burst of ``L/R`` words per lane.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    length: int = L
    n_frames: int = N_FRAMES
    #: Cycles the XSI main runs for: comfortably past the last frame.
    n_cycles: int = 2000
    #: The module's timing.  Left ``None``, both come from :func:`measured_timing` for ``length``
    #: (the mean isolated-frame latency, rounded, and the back-to-back interval).
    latency_cycles: int | None = None
    ii_cycles: int | None = None
    #: Bits only: no timing at all, as for the XSI runs that do the measuring.
    untimed: bool = False
    #: Idle cycles between frames on every input lane (both backends).  0 = back to back.
    burst_gap_cycles: int = 0
    #: Per-frame gaps (entry k: before frame k + 1), overriding ``burst_gap_cycles`` while they last.
    burst_gaps: list = field(default_factory=list)
    root: Path | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.untimed:
            self.latency_cycles = self.ii_cycles = None
        elif self.latency_cycles is None and self.ii_cycles is None:
            t = measured_timing(self.length)
            if t is not None:
                self.latency_cycles = int(round(t["latency_mean"]))
                self.ii_cycles = int(t["ii_cycles"])
        self.dut = VitisFft(name="vitis_fft", sim=self.sim, clk=self.clk, L=self.length,
                            in_w=IN_W, in_i=IN_I, tw_w=TW_W, tw_i=TW_I,
                            latency_cycles=self.latency_cycles, ii_cycles=self.ii_cycles)
        in_bw, out_bw = 2 * IN_W, 2 * int(self.dut.out_fmt.W)
        per_lane = self.length // R
        self.drivers = [StreamDriver(name=f"drv_{j}", sim=self.sim, bitwidth=in_bw,
                                     has_tlast=True, in_bundle=f"vectors/s_in_{j}", root=self.root,
                                     burst_gap_cycles=int(self.burst_gap_cycles),
                                     burst_gaps=[int(g) for g in self.burst_gaps])
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
def input_frames(n_frames: int = N_FRAMES, length: int = L, seed: int = 0):
    """``n_frames`` random full-scale complex frames, as stored ``ap_fixed<IN_W, IN_I>`` integers."""
    rng = np.random.default_rng(seed)
    lim = 1 << (IN_W - 1)
    return [(rng.integers(-lim, lim, length), rng.integers(-lim, lim, length))
            for _ in range(n_frames)]


def write_scenario(root, n_frames: int = N_FRAMES, length: int = L, seed: int = 0) -> None:
    """One bundle per input lane under ``<root>/vectors``, one burst per frame."""
    for j in range(R):
        bursts = [_pack_complex(x_re[j::R], x_im[j::R], IN_W)
                  for x_re, x_im in input_frames(n_frames, length, seed)]
        write_burst_bundle(bursts, Path(root) / "vectors" / f"s_in_{j}")


def golden(n_frames: int = N_FRAMES, length: int = L, seed: int = 0):
    """The expected output of every frame, ``[(re, im), ...]``, from the bit-exact model."""
    fn = fft_model.fft16 if length == 16 else (
        lambda xr, xi, *a: fft_model.fft_general(xr, xi, length, *a))
    out = []
    for x_re, x_im in input_frames(n_frames, length, seed):
        y_re, y_im, _ = fn(x_re, x_im, IN_W, IN_I, TW_W, TW_I)
        out.append((y_re, y_im))
    return out


def frames_from_lanes(lanes: list[np.ndarray], out_w: int, length: int = L):
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


def run_pysim(root: Path = HERE, **kw) -> VitisFftTB:
    """Write the scenario under ``<root>/vectors`` and run the testbench in SimPy."""
    write_scenario(root, kw.get("n_frames", N_FRAMES), kw.get("length", L))
    tb = VitisFftTB(name="tb", sim=Simulation(), root=Path(root), **kw)
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


if __name__ == "__main__":
    tb = run_pysim()
    got, want = pysim_output(tb), golden()
    ok = all(np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]) for g, w in zip(got, want))
    print(f"{len(got)} frames, bit-exact vs golden: {ok}")
    print("frames done at cycles:", [round(c) for c in pysim_frame_cycles(tb)])
