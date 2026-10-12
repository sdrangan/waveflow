"""rt_sounder.py — a channel sounder in a ray-traced street canyon.

The worked example for :mod:`waveflow.rfenv` (``plans/sionna_rt/sionna_rt.md``, M5).  A transmitter
on a rooftop and a receiver at street level in Sionna RT's ``simple_street_canyon``, 3.5 GHz,
traced once into a committed channel file; then the classic sounding experiment through the RF
chain Waveflow already models:

* **channel mode** -- a known Zadoff-Chu sequence played into an ``Rfdc`` DAC, through
  :class:`~waveflow.rfenv.nodes.RtChannel` (the air), back through the ``Rfdc`` ADC, and correlated:
  the estimate is the ray-traced channel's taps (AC7);
* **source mode** -- :class:`~waveflow.rfenv.nodes.RtSignalSource` feeding the ADC side of the
  loopback example's I/Q pass-through, replayed at RTL from its rendered bundle (AC6).

**Why this scene and these positions** (step 14): five paths (sionna-rt 2.2.0, ``max_depth = 3``).
Four arrive within 13 ns -- the strongest at 242.1 ns, one 10.6 ns later 3.3 dB down, two weaker
ones at −17 and −19 dB -- and spread over lags 23 to 37 at 122.88 MSa/s (8.14 ns a sample); the
fifth, at 355.0 ns and 24.7 dB down, is a separate echo at lags 43-44, 13 samples after the cluster
and inside AC7's −30 dB window.  So the sounder must resolve both a dense cluster and a distant tap.

**Why ``FILT_LEN = 256``.**  A path at a fractional delay spreads into sinc sidelobes, so a channel
has taps above −30 dB well away from its paths.  The node realizes each delay with a Kaiser-windowed
sinc; Sionna's taps use the exact sinc.  The window attenuates far sidelobes: with the default 64
taps this scene's taps above −30 dB differ from Sionna's by up to 1.49 dB, with 256 by 0.125 dB
(measured at step 14), which leaves AC7's 0.5 dB bound for the converters' quantization.  The cost is
a longer declared latency, D = 128 samples, which the sounder subtracts with the converter hop.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.rf_loopback.rf_loopback import RfSampPassThrough
from examples.rf_loopback.rf_loopback_xsi import (
    XSI_BLKSIZE,
    XSI_IQ_SAMP_RATE,
    XSI_IQ_WORD,
    XSI_NBLK,
)
from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import pack, unpack
from waveflow.rfenv.apply import taps
from waveflow.rfenv.cache import load_or_trace
from waveflow.rfenv.nodes import RtChannel, RtSignalSource
from waveflow.rfenv.noise import dbm_to_w, gain_db_for_dbfs
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec, SolverSpec
from waveflow.rfenv.waveforms import ZadoffChu
from waveflow.simulation.rf_tb import RfDataSink
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

HERE = Path(__file__).resolve().parent
#: The committed channel file's stem (``.npz`` + ``.json``).
CHANNEL = HERE / "data" / "street_canyon"

#: The scene: Sionna RT's street canyon, a rooftop transmitter and a street-level receiver.
SPEC = SceneSpec(
    scene="simple_street_canyon",
    frequency_hz=3.5e9,
    transmitters=(DeviceSpec("tx", (-30.0, 0.0, 20.0)),),
    receivers=(DeviceSpec("rx", (40.0, 5.0, 1.5)),),
    solver=SolverSpec(max_depth=3),
)

#: Channel mode's grid: the plan's default test case, 122.88 MSa/s.
SAMP_RATE = 122.88e6
BLKSIZE = 256
#: The 4x2's I/Q word: 2 complex samples a 64-bit beat -- the loopback I/Q gate's word.
WORD = XSI_IQ_WORD
#: The converter's amplitude reference, both directions.
FULL_SCALE = 1.0
#: The interpolator length (see the module doc) and its declared delay.
FILT_LEN = 256
#: The sounding sequence: a Zadoff-Chu sequence of prime length, root coprime with it.
ZC = ZadoffChu(length=1021, root=25)
#: The DAC drives the sequence at half full scale; ``tx_gain_db = 0`` reads DAC units as sqrt(W).
ZC_AMPLITUDE = 0.5
#: Where AC7 puts the strongest tap at the ADC.
TARGET_DBFS = -10.0
#: Taps compared: every one within this many dB of the strongest.
TAP_WINDOW_DB = 30.0


#: Words per converter block.
BLK_WORDS = BLKSIZE // int(WORD.samp_per_word)
#: Blocks each RF grid runs for in channel mode: enough for the startup (one converter hop, D, the
#: longest path and the interpolator's span: under 600 samples) and one whole ZC period after it.
N_BLK = 12
#: Where the correlation window starts, on the ADC's sample counter: past the startup, so every
#: sample in the window saw a steady, periodic input.
WINDOW_START = 4 * BLKSIZE


def channel() -> PathSet:
    """The committed channel file, checked against :data:`SPEC` and never traced again here."""
    return load_or_trace(SPEC, CHANNEL, retrace="never")


def channel_taps(
    paths: PathSet, n_lags: int, samp_rate: float = SAMP_RATE
) -> np.ndarray:
    """Sionna's taps of ``paths`` at lags ``0 .. n_lags - 1`` (bandwidth = the sample rate), 1-D."""
    return taps(paths, samp_rate, 0, int(n_lags) - 1)[0, 0, 0]


def gain_for_target(paths: PathSet, samp_rate: float, tx_amplitude: float) -> float:
    """The receive gain (amplitude dB) that puts the strongest tap's share of a constant-envelope
    waveform of ``tx_amplitude`` (sqrt(W)) at :data:`TARGET_DBFS`."""
    n = math.ceil(paths.max_delay() * samp_rate) + 8
    h_max = float(np.abs(channel_taps(paths, n, samp_rate)).max())
    return gain_db_for_dbfs((h_max * tx_amplitude) ** 2, TARGET_DBFS, FULL_SCALE)


def sounder_gain_db(paths: PathSet) -> float:
    """Channel mode's gain: the DAC drives the ZC at :data:`ZC_AMPLITUDE` of full scale."""
    return gain_for_target(paths, SAMP_RATE, ZC_AMPLITUDE * FULL_SCALE)


# ---------------------------------------------------------------------------
# Channel mode: ZC -> DAC -> RtChannel -> ADC
# ---------------------------------------------------------------------------


def _quantize(x: np.ndarray) -> np.ndarray:
    """Normalized complex samples -> the converter's stored integers (I and Q separately)."""
    st = WORD.samp_type()
    re = np.asarray(from_real(x.real, st), dtype=np.int64)
    im = np.asarray(from_real(x.imag, st), dtype=np.int64)
    return re + 1j * im


def _dequantize(stored: np.ndarray) -> np.ndarray:
    """The converter's stored integers -> normalized complex samples."""
    st = WORD.samp_type()
    re = to_real(array(st, np.real(stored).astype(np.int64)))
    im = to_real(array(st, np.imag(stored).astype(np.int64)))
    return re + 1j * im


def zc_words(n_blk: int = N_BLK) -> list[np.ndarray]:
    """The ZC at :data:`ZC_AMPLITUDE`, quantized and packed as the DAC's AXIS words, one burst per
    block: what the fabric hands ``Rfdc.tx_streams[0]``."""
    x = ZC_AMPLITUDE * ZC.samples(0, int(n_blk) * BLKSIZE, SAMP_RATE)
    words = np.asarray(pack(WORD, _quantize(x).reshape(1, -1)), dtype=np.uint64)[0]
    return [words[k * BLK_WORDS : (k + 1) * BLK_WORDS] for k in range(int(n_blk))]


def dac_samples(n_blk: int = N_BLK) -> np.ndarray:
    """What the DAC puts on the air for :func:`zc_words`: the dequantized ZC, in sqrt(W)."""
    words = np.concatenate(zc_words(n_blk)).reshape(1, -1)
    return _dequantize(unpack(WORD, words))[0] * FULL_SCALE


def write_scenario(root, n_blk: int = N_BLK, name: str = "zc") -> None:
    """Materialize ``<root>/vectors/<name>``: the ZC bursts the driver plays."""
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(zc_words(n_blk), Path(root) / "vectors" / name)


@dataclass
class RtSounderTB(FreeRunMod):
    """Channel mode: one converter, both directions, and the ray-traced air between them::

        StreamDriver -> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RtChannel
                                                                          |
        StreamSink <-- Rfdc.rx_streams[0] | Rfdc.rx_rf <--RFSampIF--------+

    Wired like ``examples/rf_shot_loopback`` with :class:`~waveflow.rfenv.nodes.RtChannel` in place
    of ``RfSampDelay``: one ``Rfdc`` owns both epochs, so a sample's ADC index minus its DAC index is
    the loop's declared latency plus the path's delay.
    """

    #: Blocks each RF grid runs for.
    n_blk: int = N_BLK
    #: The bundle the driver plays (see :func:`write_scenario`).
    in_bundle: str = "vectors/zc"
    #: The channel's receive gain; ``None``: :func:`sounder_gain_db` (the strongest tap at -10 dBFS).
    gain_db: float | None = None
    #: The channel's noise figure; ``None`` (AC7) turns thermal noise off.
    noise_figure_db: float | None = None
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    potential_targets: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=RFSOC4X2_CLK_HZ)
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=SAMP_RATE)
        self.blk_period = BLKSIZE / SAMP_RATE
        #: The channel applied (the committed file).
        self.paths = channel()
        if self.gain_db is None:
            self.gain_db = sounder_gain_db(self.paths)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=1, word=WORD,
                         full_scale=FULL_SCALE, t0_rx=0.0, t0_tx=0.0)  # fmt: skip
        self.chan = RtChannel(name=f"{self.name}_chan", sim=self.sim, pathset=self.paths,
                              filt_len=FILT_LEN, gain_db=float(self.gain_db),
                              noise_figure_db=self.noise_figure_db)  # fmt: skip
        w = self.rfdc.axis_bitwidth
        self.drv = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                in_bundle=str(self.in_bundle), has_tlast=True)  # fmt: skip
        # Two blocks of headroom: the ADC offers at the converter's rate and drops what the
        # fabric does not take, which here would be measuring the sink.
        self.snk = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=w,
                              out_bundle="vectors/rx", has_tlast=True,
                              queue_size=2 * BLK_WORDS)  # fmt: skip
        for c in (self.rfdc, self.chan, self.drv, self.snk):
            self.add_comp(c)

        # The RF domain: the air is the node between the two edges, never on either.
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=BLKSIZE, n_blk=int(self.n_blk),
                               complex_samp=True)  # fmt: skip
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.chan.rf_in)
        self.add_if(self.dac_if)
        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=BLKSIZE, n_blk=int(self.n_blk),
                               complex_samp=True)  # fmt: skip
        self.adc_if.bind("tx", self.chan.rf_out)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        # The PL domain.
        for nm, master, slave in (
            ("dac", self.drv.stream_ep, self.rfdc.tx_streams[0]),
            ("adc", self.rfdc.rx_streams[0], self.snk.stream_ep),
        ):
            ifc = StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                           bitwidth=w, depth=2 * BLK_WORDS)  # fmt: skip
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_axis", ifc)

    @property
    def loop_blk_latency(self) -> int:
        """Block periods from the DAC's grid to the ADC's: one converter hop (a block exists at its
        tick and crosses the following period) plus what the channel declares (zero)."""
        return 1 + int(self.chan.blk_latency)

    @property
    def loop_latency_samp(self) -> int:
        """Samples from a DAC index to the ADC index of a zero-delay path: the hop and the
        channel's interpolator delay D, each counted once."""
        return int(self.loop_blk_latency) * BLKSIZE + int(self.chan.latency_samp)

    @property
    def run_until(self) -> float:
        """The grids' own length plus a two-block tail (both converters are free-running)."""
        return (int(self.n_blk) + 2) * self.blk_period


def run_channel_mode(root=None, **kw) -> RtSounderTB:
    """Build the channel-mode graph, run it to the grids' horizon, return the testbench."""
    tb = RtSounderTB(name="tb", sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, tb.n_blk, name=Path(tb.in_bundle).name)
        tb.drv.root = base
        tb.snk.root = base
        sim = tb.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            proc = obj.run_proc()
            if proc is not None:
                sim.env.process(proc)
        try:
            sim.env.run(until=tb.run_until)
        except Exception:
            for obj in sim._sim_objs:
                obj.error_cleanup()
            raise
        for obj in sim._sim_objs:
            obj.post_sim()
    return tb


def adc_samples(tb: RtSounderTB) -> np.ndarray:
    """The ADC's capture as normalized complex samples, by the ADC's sample counter from 0."""
    words = np.concatenate(
        [np.asarray(b, dtype=np.uint64).ravel() for b in tb.snk.words]
    )
    return _dequantize(unpack(WORD, words.reshape(1, -1)))[0] * FULL_SCALE


def sounding_lags(paths: PathSet) -> np.ndarray:
    """The lags one ZC period resolves, as the circle maps them: from the last lag the
    interpolator can reach (the longest path plus ``FILT_LEN / 2``) back round to negative lags.
    """
    n = ZC.length
    hi = math.ceil(paths.max_delay() * SAMP_RATE) + FILT_LEN // 2
    lags = np.arange(n)
    return np.where(lags <= hi, lags, lags - n)


def estimate_taps(tb: RtSounderTB) -> np.ndarray:
    """The channel's taps at :func:`sounding_lags`, from the capture, in sqrt(W) per sqrt(W).

    One ZC period of the ADC capture from :data:`WINDOW_START`, deconvolved circularly by the same
    period of what the DAC transmitted (the dequantized words, so the DAC's quantization is not an
    error), shifted by the loop's declared latency, and divided by the receive gain.  The ZC's
    periodic autocorrelation is ideal, so this is the circular channel response exactly; the
    interpolator's support (``FILT_LEN``) is shorter than the period, so nothing wraps onto a lag
    it does not belong to.  Entry ``i`` is lag ``sounding_lags(...)[i]``.
    """
    n = ZC.length
    y = adc_samples(tb)
    if y.size < WINDOW_START + n:
        raise AssertionError(
            f"the capture holds {y.size} samples; the window needs {WINDOW_START + n}. "
            f"Raise n_blk."
        )
    y = y[WINDOW_START : WINDOW_START + n]
    # The DAC sample that reaches ADC sample WINDOW_START over a zero-delay path.
    s = WINDOW_START - tb.loop_latency_samp
    x = dac_samples(tb.n_blk)[s : s + n]
    h_circ = np.fft.ifft(np.fft.fft(y) / np.fft.fft(x))
    return h_circ / (10.0 ** (float(tb.gain_db) / 20.0))


@dataclass
class TapComparison:
    """AC7's comparison.  Levels are dB relative to Sionna's strongest tap."""

    #: ``(lag, Sionna dB, estimate dB, estimate - Sionna dB)`` for every Sionna tap within
    #: :data:`TAP_WINDOW_DB` of the strongest.
    rows: list[tuple[int, float, float, float]]
    #: The strongest estimated tap at a lag where Sionna's is below the window: ``(lag, dB)``.
    spurious: tuple[int, float]
    #: Sionna's strongest lag and the estimate's.
    peak_lags: tuple[int, int]

    @property
    def worst_db(self) -> float:
        return max(abs(r[3]) for r in self.rows)

    def table(self) -> str:
        head = "lag   Sionna    sounder   diff (dB, re Sionna's strongest tap)"
        body = [
            f"{lag:3d}  {w:7.2f}  {g:8.2f}  {d:+7.3f}" for lag, w, g, d in self.rows
        ]
        tail = (
            f"worst |diff| {self.worst_db:.3f} dB; strongest lag Sionna {self.peak_lags[0]}, "
            f"sounder {self.peak_lags[1]}; strongest tap off Sionna's set: lag "
            f"{self.spurious[0]} at {self.spurious[1]:.2f} dB"
        )
        return "\n".join([head, *body, tail])


def compare_taps(paths: PathSet, h_est: np.ndarray) -> TapComparison:
    """Compare an estimate at :func:`sounding_lags` with ``rfenv.taps`` at the same lags."""
    lags = sounding_lags(paths)
    order = np.argsort(lags)
    want = np.empty(lags.size)
    want[order] = (
        np.abs(taps(paths, SAMP_RATE, int(lags.min()), int(lags.max()))[0, 0, 0]) ** 2
    )
    got = np.abs(np.asarray(h_est)) ** 2
    ref = want.max()
    w_db = 10 * np.log10(want / ref)
    g_db = 10 * np.log10(np.maximum(got, 1e-300) / ref)
    inside = w_db > -TAP_WINDOW_DB
    rows = [
        (int(lags[i]), float(w_db[i]), float(g_db[i]), float(g_db[i] - w_db[i]))
        for i in sorted(np.flatnonzero(inside), key=lambda i: lags[i])
    ]
    out = np.flatnonzero(~inside)
    j = out[np.argmax(g_db[out])]
    return TapComparison(
        rows=rows,
        spurious=(int(lags[j]), float(g_db[j])),
        peak_lags=(int(lags[np.argmax(want)]), int(lags[np.argmax(got)])),
    )


# ---------------------------------------------------------------------------
# Source mode: RtSignalSource -> ADC -> pass-through -> DAC -> sink
# ---------------------------------------------------------------------------
#
# What a receiver in the street hears, fed to the RF loopback's fabric.  The geometry is the loopback
# I/Q gate's (plan §14, M4 review), so the XSI replay drives the RTL that gate already built and is
# judged against the transient and drops it recorded.

#: Source mode's sample rate: the I/Q gate's, half the real loopback's (see ``XSI_IQ_SAMP_RATE``).
SOURCE_SAMP_RATE = XSI_IQ_SAMP_RATE
#: Source mode's block and run length: the I/Q gate's.
SOURCE_BLKSIZE = XSI_BLKSIZE
SOURCE_NBLK = XSI_NBLK
#: The transmitter: the ZC at 30 dBm (1 W), the street canyon's rooftop.
SOURCE_TX_POWER_DBM = 30.0
#: The receiver's noise figure: thermal noise on, so the ADC sees a real received signal (about
#: 36 dB SNR here) and the replay has to carry the noise bit for bit too.
SOURCE_NOISE_FIGURE_DB = 7.0
#: The source's rendered bundle and the sink's capture, relative to the run root.  Their own names:
#: the XSI workspace holds other scenarios.
IN_BUNDLE_SRC = "vectors/rt_src"
OUT_BUNDLE_SRC = "vectors/rt_out"
#: The source-mode testbench's name, in both backends.  Not cosmetic: the source's noise stream is
#: keyed by its node's name, so a pysim graph and an XSI graph named differently would render
#: different bundles.
SOURCE_TB_NAME = "src_tb"


def source_gain_db(paths: PathSet) -> float:
    """Source mode's gain: the strongest tap's share of the 30 dBm ZC at :data:`TARGET_DBFS`."""
    amp = math.sqrt(dbm_to_w(SOURCE_TX_POWER_DBM))
    return gain_for_target(paths, SOURCE_SAMP_RATE, amp)


@dataclass
class RtSourceTB(FreeRunMod):
    """Source mode: the ray-traced received signal through the RF loopback's fabric::

        RtSignalSource --RFSampIF--> Rfdc.rx_rf | rx_streams[0] --StreamIF--> RfSampPassThrough
                                                                                    |
        RfDataSink     <--RFSampIF-- Rfdc.tx_rf | tx_streams[0] <--StreamIF---------+

    ``examples/rf_loopback``'s ``RfLoopbackTB`` with :class:`~waveflow.rfenv.nodes.RtSignalSource`
    in place of ``RfDataSource`` (that class builds its own source, and existing examples do not
    change).  The DUT is ``rf_pass_through``, imported: its RTL is word-agnostic, so the replay
    needs no RTL of its own.
    """

    #: Blocks the RF grids run for, and the source sends.
    n_blk: int = SOURCE_NBLK
    blksize: int = SOURCE_BLKSIZE
    samp_rate: float = SOURCE_SAMP_RATE
    #: The source's rendered bundle (the XSI twin replays it) and the sink's capture.
    in_bundle: str = IN_BUNDLE_SRC
    out_bundle: str = OUT_BUNDLE_SRC
    #: Receiver queue depth at the sink, in blocks (the loopback's).
    sink_depth: int = 2
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    potential_targets: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=RFSOC4X2_CLK_HZ)
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.blk_period = int(self.blksize) / float(self.samp_rate)
        #: The channel the source applies (the committed file).
        self.paths = channel()

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=1, word=WORD,
                         full_scale=FULL_SCALE, t0_rx=0.0, t0_tx=0.0)  # fmt: skip
        w = self.rfdc.axis_bitwidth
        self.nwords_blk = int(self.blksize) // int(WORD.samp_per_word)
        self.dut = RfSampPassThrough(name=f"{self.name}_dut", sim=self.sim, bitwidth=w, n_ch=1,
                                     nwords_blk=self.nwords_blk, clk=self.axis_clk)  # fmt: skip
        #: Block periods from the ADC edge to the DAC edge: the ADC's hop plus the DUT's own --
        #: the DAC edge's entitled startup transient (as ``RfLoopbackTB``).
        self.loop_blk_latency = 1 + int(self.dut.blk_latency)
        self.source = RtSignalSource(
            name=f"{self.name}_src",
            sim=self.sim,
            in_bundle=str(self.in_bundle),
            waveform=ZC,
            tx_power_dbm=SOURCE_TX_POWER_DBM,
            pathset=self.paths,
            filt_len=FILT_LEN,
            gain_db=source_gain_db(self.paths),
            noise_figure_db=SOURCE_NOISE_FIGURE_DB,
            n_blk=int(self.n_blk),
        )
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim,
                               out_bundle=str(self.out_bundle),
                               depth=int(self.sink_depth))  # fmt: skip
        for c in (self.dut, self.rfdc, self.source, self.sink):
            self.add_comp(c)

        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk),
                               complex_samp=True)  # fmt: skip
        self.adc_if.bind("tx", self.source.rf_ep)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk),
                               complex_samp=True)  # fmt: skip
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)

        # The PL domain, as RfLoopbackTB: no depth on the ADC side (it becomes the DUT's top-level
        # port, which cannot carry one), a block and slack on the DAC side (a testbench channel).
        self.adc_axis = StreamIF(name=f"{self.name}_adc_axis", sim=self.sim, clk=self.axis_clk,
                                 bitwidth=w)  # fmt: skip
        self.adc_axis.bind("master", self.rfdc.rx_streams[0])
        self.adc_axis.bind("slave", self.dut.s_ins[0])
        self.add_if(self.adc_axis)
        self.dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.axis_clk,
                                 bitwidth=w, depth=2 * self.nwords_blk)  # fmt: skip
        self.dac_axis.bind("master", self.dut.s_outs[0])
        self.dac_axis.bind("slave", self.rfdc.tx_streams[0])
        self.add_if(self.dac_axis)


def write_source_scenario(tb: RtSourceTB, root) -> list[np.ndarray]:
    """Render the source's bundle into ``<root>/<in_bundle>`` and point both RF peers at ``root``;
    returns the blocks written.  The single writer for both backends."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    root = Path(root)
    tb.source.render_bundle(root / tb.in_bundle)
    tb.source.root = root
    tb.sink.root = root
    return read_rf_bundle(root / tb.in_bundle, 1, int(tb.blksize), complex_samp=True)


def run_source_mode(
    root=None, **kw
) -> tuple[RtSourceTB, list[np.ndarray], list[np.ndarray]]:
    """Render, run the source-mode graph, read the capture back; returns
    ``(tb, rendered blocks, captured blocks)``."""
    from waveflow.simulation.rf_tb import read_rf_bundle

    tb = RtSourceTB(name=SOURCE_TB_NAME, sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        sent = write_source_scenario(tb, base)
        tb.sim.run_sim()
        got = read_rf_bundle(
            base / tb.out_bundle, 1, int(tb.blksize), complex_samp=True
        )
    return tb, sent, got


def adc_quantized(block: np.ndarray) -> np.ndarray:
    """What the ADC makes of an RF block and the DAC plays back: quantized, in sqrt(W)."""
    return _dequantize(_quantize(np.asarray(block) / FULL_SCALE)) * FULL_SCALE


def main() -> None:
    """Channel mode: sound the street canyon and print AC7's table."""
    tb = run_channel_mode()
    print(
        f"{SPEC.scene}, {SPEC.frequency_hz / 1e9:g} GHz, {SAMP_RATE / 1e6:g} MSa/s; gain_db "
        f"{tb.gain_db:.2f}; loop latency {tb.loop_latency_samp} samples"
    )
    print(compare_taps(tb.paths, estimate_taps(tb)).table())


if __name__ == "__main__":
    main()
