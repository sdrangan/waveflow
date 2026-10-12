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

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord, pack, unpack
from waveflow.rfenv.apply import taps
from waveflow.rfenv.cache import load_or_trace
from waveflow.rfenv.nodes import RtChannel
from waveflow.rfenv.noise import gain_db_for_dbfs
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec, SolverSpec
from waveflow.rfenv.waveforms import ZadoffChu
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
#: The 4x2's I/Q word: 2 complex samples a 64-bit beat.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)
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


def channel_taps(paths: PathSet, n_lags: int) -> np.ndarray:
    """Sionna's taps of ``paths`` at lags ``0 .. n_lags - 1`` (bandwidth = the sample rate), 1-D."""
    return taps(paths, SAMP_RATE, 0, int(n_lags) - 1)[0, 0, 0]


def sounder_gain_db(paths: PathSet) -> float:
    """The receive gain that puts the strongest tap's share of the ZC at :data:`TARGET_DBFS`."""
    n = math.ceil(paths.max_delay() * SAMP_RATE) + 8
    h_max = float(np.abs(channel_taps(paths, n)).max())
    p_w = (h_max * ZC_AMPLITUDE * FULL_SCALE) ** 2
    return gain_db_for_dbfs(p_w, TARGET_DBFS, FULL_SCALE)


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
