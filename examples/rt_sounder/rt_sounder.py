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

from pathlib import Path

from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.rfenv.cache import load_or_trace
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec, SolverSpec
from waveflow.rfenv.waveforms import ZadoffChu

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


def channel() -> PathSet:
    """The committed channel file, checked against :data:`SPEC` and never traced again here."""
    return load_or_trace(SPEC, CHANNEL, retrace="never")
