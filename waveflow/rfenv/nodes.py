"""nodes.py -- the RF environment as pysim nodes on :class:`~waveflow.hw.rf_sample_if.RFSampIF` edges.

* :class:`RtChannel` -- a **path** between two converter edges, like
  :class:`~waveflow.simulation.rf_tb.RfSampDelay`, at multipath fidelity: every path's amplitude,
  sub-sample delay and Doppler shift, per antenna pair, plus thermal noise.  Between an ``Rfdc``'s
  ``tx_rf`` and ``rx_rf`` it is the air between the antennas.
* :class:`RtSignalSource` -- what a receiver hears: a waveform (:mod:`waveflow.rfenv.waveforms`) at
  a transmit power, through the channel, plus noise, onto an edge, like
  :class:`~waveflow.simulation.rf_tb.RfDataSource`.  Its XSI twin is ``RfDataSource``'s
  ``RfFileSource``, replaying the bundle :meth:`RtSignalSource.render_bundle` writes from the same
  computation -- no new C++, and the same bytes in both backends.

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

from waveflow.hw.hw_module import DynParam, HwModule
from waveflow.hw.rf_sample_if import DEFAULT_RF_RX_DEPTH, RFSampIFRx, RFSampIFTx
from waveflow.rfenv.apply import DEFAULT_BETA, DEFAULT_FILT_LEN, ChannelApplier
from waveflow.rfenv.noise import (
    ThermalNoise,
    amplitude_gain,
    dbm_to_w,
    stream_tag,
    thermal_noise_power,
)
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import SceneSpec
from waveflow.rfenv.waveforms import Waveform
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


@dataclass
class RtSignalSource(HwModule):
    """A waveform through a channel, at a power, with noise: what the receiving antennas deliver.

    Endpoint ``rf_ep`` (the producer of a complex edge whose ``n_ch`` is the channel's receive
    channel count).  It computes block ``k`` from absolute sample ``(k - 1) * blksize`` on: the
    waveform at that index, scaled to ``tx_power_dbm`` (per transmit channel), through the channel,
    plus thermal noise, times ``gain_db``.  The same computation, offline, is
    :meth:`render_bundle`, so pysim and the XSI replay of the bundle carry the same samples.

    **Only** ``in_bundle`` **is a** :class:`~waveflow.hw.hw_module.DynParam`: the XSI generator
    emits every non-default ``DynParam`` as a member assignment on the C++ model, and
    ``RfFileSource`` has that one field (``plans/sionna_rt/sionna_rt.md``, Rule 13).
    """

    #: The rendered bundle the XSI twin replays (:meth:`render_bundle` writes it).
    in_bundle: DynParam[str] = ""
    #: Anchor for a relative ``channel``.
    root: Path | None = None
    #: What each transmit channel sends; ``None`` is a carrier at the centre frequency.
    waveform: Waveform | None = None
    #: Transmit power per transmit channel, dBm (the built-in waveforms have unit power).
    tx_power_dbm: float = 0.0
    #: The channel: exactly one of these, as for :class:`RtChannel`.
    pathset: PathSet | None = None
    channel: str | Path | None = None
    spec: SceneSpec | None = None
    mode: str = "cached"
    filt_len: int = DEFAULT_FILT_LEN
    beta: float = DEFAULT_BETA
    delay_mode: str = "absolute"
    #: The receive chain's gain onto the edge's units (amplitude dB), after the noise.
    gain_db: float = 0.0
    #: Receiver noise figure in dB; ``None`` turns thermal noise off.
    noise_figure_db: float | None = None
    temperature_k: float = 290.0
    seed: int = 0
    #: Blocks to send, then stop; ``None`` sends until the run ends.
    n_blk: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFTx(sim=self.sim)
        self.add_endpoint(self.rf_ep)
        #: The channel applied, set in ``pre_sim`` (or by :meth:`render_bundle`).
        self.paths: PathSet | None = None
        self.n_out = 0

    @property
    def latency_samp(self) -> int:
        """Samples every path is delayed beyond its own delay: ``filt_len / 2``."""
        return int(self.filt_len) // 2

    def _resolve(self) -> PathSet:
        if self.paths is None:
            self.paths = resolve_channel(
                pathset=self.pathset,
                channel=self.channel,
                spec=self.spec,
                mode=self.mode,
                root=self.root,
                who=f"RtSignalSource '{self.name}'",
            )
        return self.paths

    def blocks(self, blksize: int, samp_rate: float):
        """Yield the source's blocks, ``(n_rx_ch, blksize)`` each, from sample 0 on, forever."""
        ps = self._resolve()
        app = ChannelApplier(
            ps,
            float(samp_rate),
            filt_len=int(self.filt_len),
            beta=float(self.beta),
            delay_mode=self.delay_mode,
            start_n0=0,
        )
        noise = None
        if self.noise_figure_db is not None:
            p = thermal_noise_power(
                float(samp_rate), float(self.noise_figure_db), float(self.temperature_k)
            )
            noise = ThermalNoise(p, seed=int(self.seed), tag=stream_tag(self.name))
        amp = float(np.sqrt(dbm_to_w(self.tx_power_dbm)))
        g = amplitude_gain(self.gain_db)
        n0 = 0
        while True:
            x = self._transmitted(n0, int(blksize), float(samp_rate), ps.n_tx_ch)
            y = app.process(amp * x, n0)
            if noise is not None:
                y = y + noise.samples(y.shape[0], n0, y.shape[1])
            yield g * y
            n0 += int(blksize)

    def _transmitted(
        self, n0: int, count: int, samp_rate: float, n_tx: int
    ) -> np.ndarray:
        if self.waveform is None:
            return np.ones((n_tx, count), dtype=np.complex128)
        x = np.asarray(self.waveform.samples(n0, count, samp_rate), dtype=np.complex128)
        if x.ndim == 1:
            return np.broadcast_to(x, (n_tx, count))
        if x.shape[0] != n_tx:
            raise ValueError(
                f"RtSignalSource '{self.name}': the waveform has {x.shape[0]} channels, the "
                f"channel {n_tx} transmit channels"
            )
        return x

    def pre_sim(self) -> None:
        ps = self._resolve()
        ep = self.rf_ep
        if not ep.complex_samp:
            raise ValueError(
                f"RtSignalSource '{self.name}': the edge must be complex baseband"
            )
        if int(ep.n_ch) != ps.n_rx_ch:
            raise ValueError(
                f"RtSignalSource '{self.name}': the channel has {ps.n_rx_ch} receive channels, "
                f"the edge carries {ep.n_ch}"
            )

    def run_proc(self) -> ProcessGen[None]:
        gen = self.blocks(int(self.rf_ep.blksize), float(self.rf_ep.samp_rate))
        while self.n_blk is None or self.n_out < int(self.n_blk):
            yield from self.rf_ep.put(next(gen))
            self.n_out += 1

    def render_bundle(
        self, bundle_dir: str | Path, n_blk: int, blksize: int, samp_rate: float
    ) -> Path:
        """Write the first ``n_blk`` blocks as a complex RF bundle -- what the XSI twin replays."""
        from waveflow.simulation.rf_tb import write_rf_bundle

        gen = self.blocks(int(blksize), float(samp_rate))
        blocks = [next(gen) for _ in range(int(n_blk))]
        return write_rf_bundle(blocks, bundle_dir, complex_samp=True)

    def bfm_model(self):
        """XSI twin: ``RfFileSource`` replaying ``in_bundle``, sized as ``RfDataSource`` sizes it.

        One block in **components** (a complex sample is two doubles) and the element kind, which
        the C++ model checks against the bundle's manifest.
        """
        from waveflow.build.composite_gen import BfmModel

        comps = int(self.rf_ep.n_ch) * int(self.rf_ep.blksize) * 2
        return BfmModel("RfFileSource", ports=("rf_ep",), extra_args=(str(comps), "1"))
