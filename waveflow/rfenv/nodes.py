"""nodes.py -- the RF environment as pysim nodes on :class:`~waveflow.hw.rf_sample_if.RFSampIF` edges.

* :class:`RtChannel` -- a **path** between two converter edges, like
  :class:`~waveflow.simulation.rf_tb.RfSampDelay`, at multipath fidelity: every path's amplitude,
  sub-sample delay and Doppler shift, per antenna pair, plus thermal noise.  Between an ``Rfdc``'s
  ``tx_rf`` and ``rx_rf`` it is the air between the antennas.

Both read their channel from one of three places (:func:`resolve_channel`): a
:class:`~waveflow.rfenv.paths.PathSet` handed over directly, a channel file, or a
:class:`~waveflow.rfenv.sionna_backend.SceneSpec` -- cached through
:func:`~waveflow.rfenv.cache.load_or_trace`, or traced at ``pre_sim`` in **live** mode.  Everything
channel-dependent is checked in ``pre_sim``, because in live mode there is no channel before then.

**Units** (design note, ``plans/sionna_rt/sionna_rt.md`` §14, items 1 and A4): the input is read as
the complex envelope at the transmit antenna, ``tx_gain_db`` mapping its units to sqrt(W); the output
is ``gain_db`` (amplitude) times the received envelope plus thermal noise, in the units the receiving
converter's ``full_scale`` is set for.

**Timing.**  One block in, one block out, in the same SimPy event (``blk_latency = 0``).  The
interpolator's look-ahead is a declared delay of ``filt_len / 2`` samples on every path
(:attr:`RtChannel.latency_samp`), the number a loop subtracts, as it adds ``RfSampDelay``'s.  A
block's samples are placed by the edge's absolute grid index (``RfBlock.idx``), so a block the
receiver queue dropped -- or never got, before the first one -- is a zero-filled, counted gap, and
the delays, the Doppler phase and the noise stay on the grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.hw.hw_module import HwModule
from waveflow.hw.rf_sample_if import DEFAULT_RF_RX_DEPTH, RFSampIFRx, RFSampIFTx
from waveflow.rfenv.apply import DEFAULT_BETA, DEFAULT_FILT_LEN, ChannelApplier
from waveflow.rfenv.noise import (
    ThermalNoise,
    amplitude_gain,
    stream_tag,
    thermal_noise_power,
)
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import SceneSpec
from waveflow.simulation.simobj import ProcessGen

#: Where a channel comes from.
MODES = ("cached", "live")


def resolve_channel(
    *,
    pathset: PathSet | None,
    channel: str | Path | None,
    spec: SceneSpec | None,
    mode: str,
    root: Path | None,
    who: str,
) -> PathSet:
    """The :class:`PathSet` a node applies, from exactly one of its three sources.

    * ``pathset`` -- used as it is;
    * ``spec`` with ``mode="live"`` -- traced now (needs Sionna RT);
    * ``spec`` in ``cached`` mode -- :func:`~waveflow.rfenv.cache.load_or_trace` at ``channel``;
    * ``channel`` alone -- a channel file, loaded.

    A relative ``channel`` resolves against ``root`` (or the process's working directory).
    """
    if mode not in MODES:
        raise ValueError(f"{who}: mode must be one of {MODES}, got {mode!r}")
    if pathset is not None:
        if channel is not None or spec is not None:
            raise ValueError(f"{who}: give a pathset, or a channel/spec, not both")
        if mode == "live":
            raise ValueError(
                f"{who}: live mode traces a spec; a pathset is already traced"
            )
        return pathset
    stem = None
    if channel is not None:
        stem = Path(channel)
        if root is not None and not stem.is_absolute():
            stem = Path(root) / stem
    if mode == "live":
        if spec is None:
            raise ValueError(f"{who}: live mode needs a spec to trace")
        from waveflow.rfenv.sionna_backend import trace

        return trace(spec)
    if spec is not None:
        if stem is None:
            raise ValueError(
                f"{who}: a spec in cached mode needs a channel file stem to cache it in"
            )
        from waveflow.rfenv.cache import load_or_trace

        return load_or_trace(spec, stem)
    if stem is None:
        raise ValueError(
            f"{who}: no channel: give a pathset, a channel file, or a spec"
        )
    return PathSet.load(stem)


@dataclass
class RtChannel(HwModule):
    """The ray-traced (or any :class:`PathSet`) channel between two RF edges.

    Endpoints ``rf_in`` (this node receives the transmitter's edge) and ``rf_out`` (it produces the
    receiver's edge).  ``rf_in.n_ch`` is the number of transmit channels of the channel, and
    ``rf_out.n_ch`` the number of receive channels; both edges complex, at one sample rate and one
    ``blksize``.
    """

    #: The channel, handed over directly.
    pathset: PathSet | None = None
    #: A channel file stem (``<stem>.npz`` + ``.json``), or where a cached ``spec`` lives.
    channel: str | Path | None = None
    #: A scene specification: traced into ``channel`` (cached) or at ``pre_sim`` (live).
    spec: SceneSpec | None = None
    #: ``"cached"`` or ``"live"``.
    mode: str = "cached"
    #: Anchor for a relative ``channel``.
    root: Path | None = None
    #: Interpolator length and Kaiser beta (:mod:`waveflow.rfenv.apply`).
    filt_len: int = DEFAULT_FILT_LEN
    beta: float = DEFAULT_BETA
    #: ``"absolute"`` (the paths' true delays) or ``"relative"`` (the earliest path at zero delay).
    delay_mode: str = "absolute"
    #: Input amplitude units to sqrt(W) at the transmit antenna (amplitude dB).
    tx_gain_db: float = 0.0
    #: The receive chain's gain onto the output edge's units (amplitude dB), after the noise.
    gain_db: float = 0.0
    #: Receiver noise figure in dB; ``None`` turns thermal noise off.
    noise_figure_db: float | None = None
    #: Noise temperature in kelvin.
    temperature_k: float = 290.0
    #: Noise seed; the node's name tags its stream, so two channels never share one.
    seed: int = 0
    #: Block periods added beyond the interpolator's delay: zero, and declared (as ``RfSampDelay``).
    blk_latency: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_in = RFSampIFRx(sim=self.sim, depth=DEFAULT_RF_RX_DEPTH)
        self.rf_out = RFSampIFTx(sim=self.sim)
        for ep in (self.rf_in, self.rf_out):
            self.add_endpoint(ep)
        #: Blocks in and out (a path that swallowed everything and one never fed look the same).
        self.n_in = 0
        self.n_out = 0
        #: The channel applied, set in ``pre_sim``.
        self.paths: PathSet | None = None
        self._applier: ChannelApplier | None = None
        self._noise: ThermalNoise | None = None

    @property
    def latency_samp(self) -> int:
        """Samples every path is delayed beyond its own delay: ``filt_len / 2``."""
        return int(self.filt_len) // 2

    @property
    def dropped_samples(self) -> int:
        """Input samples zero-filled because their block never reached this node."""
        return 0 if self._applier is None else self._applier.dropped_samples

    def pre_sim(self) -> None:
        who = f"RtChannel '{self.name}'"
        self.paths = resolve_channel(
            pathset=self.pathset,
            channel=self.channel,
            spec=self.spec,
            mode=self.mode,
            root=self.root,
            who=who,
        )
        i, o = self.rf_in, self.rf_out
        if not (i.complex_samp and o.complex_samp):
            raise ValueError(
                f"{who}: both edges must be complex baseband (complex_samp=True)"
            )
        if i.samp_rate != o.samp_rate or int(i.blksize) != int(o.blksize):
            raise ValueError(
                f"{who}: the edges disagree -- in is {i.samp_rate} Hz x {i.blksize}, out is "
                f"{o.samp_rate} Hz x {o.blksize}; a channel neither resamples nor re-blocks"
            )
        if int(i.n_ch) != self.paths.n_tx_ch or int(o.n_ch) != self.paths.n_rx_ch:
            raise ValueError(
                f"{who}: the channel is {self.paths.n_rx_ch} rx x {self.paths.n_tx_ch} tx, but "
                f"rf_in carries {i.n_ch} and rf_out {o.n_ch} channels"
            )
        fs = float(i.samp_rate)
        self._applier = ChannelApplier(
            self.paths,
            fs,
            filt_len=int(self.filt_len),
            beta=float(self.beta),
            delay_mode=self.delay_mode,
            start_n0=0,
        )
        self._noise = None
        if self.noise_figure_db is not None:
            p = thermal_noise_power(
                fs, float(self.noise_figure_db), float(self.temperature_k)
            )
            self._noise = ThermalNoise(
                p, seed=int(self.seed), tag=stream_tag(self.name)
            )

    def run_proc(self) -> ProcessGen[None]:
        blksize = int(self.rf_in.blksize)
        g_in, g_out = amplitude_gain(self.tx_gain_db), amplitude_gain(self.gain_db)
        while True:
            blk = yield from self.rf_in.get()
            self.n_in += 1
            n0 = (int(blk.idx) - 1) * blksize
            y = self._applier.process(g_in * np.asarray(blk.data), n0)
            if self._noise is not None:
                y = y + self._noise.samples(y.shape[0], n0, y.shape[1])
            yield from self.rf_out.put(g_out * y)
            self.n_out += 1

    def assert_ran(self, *, where: str = "") -> None:
        """The channel carried blocks, and nothing is stuck inside it (as ``RfSampDelay``)."""
        if not self.n_in:
            raise AssertionError(
                f"{where}RtChannel '{self.name}' was never handed a block"
            )
        if not 0 <= self.n_in - self.n_out <= 1:
            raise AssertionError(
                f"{where}RtChannel '{self.name}' took {self.n_in} block(s) and emitted "
                f"{self.n_out}; at most one may be in flight when the horizon closes"
            )
