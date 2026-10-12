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
:func:`~waveflow.rfenv.cache.load_or_trace`, or traced at ``pre_sim`` in **live** mode.  The edges
are checked first, then the channel is resolved, then the two are checked against each other: in
live mode there is no channel before ``pre_sim``, and a wrongly wired edge should not cost a trace.

**Units** (design note, ``plans/sionna_rt/sionna_rt.md`` §14, items 1 and A4): the input is read as
the complex envelope at the transmit antenna, ``tx_gain_db`` mapping its units to sqrt(W); the output
is ``gain_db`` (amplitude) times the received envelope plus thermal noise, in the units the receiving
converter's ``full_scale`` is set for.

**Timing.**  One block in, one block out, in the same SimPy event (``blk_latency = 0``).  The
interpolator's look-ahead is a declared delay of ``filt_len / 2`` samples on every path
(``latency_samp``), the number a loop subtracts, as it adds ``RfSampDelay``'s.  ``RtChannel`` places
a block's samples by the input edge's absolute grid index (``RfBlock.idx``, the transmitter's sample
counter).  A block that never reached it -- dropped at its receive queue, before or after the first
-- is replaced by a block of zero input, and the channel's response to that zero input (the tail of
what came before) is emitted for it, so the output edge stays on its grid too.  Those blocks are
counted (:attr:`RtChannel.dropped_blocks`).
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
    channel_root: Path | None,
    who: str,
    retrace: str = "if_stale",
) -> PathSet:
    """The :class:`PathSet` a node applies, from exactly one of its three sources.

    * ``pathset`` -- used as it is;
    * ``spec`` with ``mode="live"`` -- traced now (needs Sionna RT); no ``channel``;
    * ``spec`` in ``cached`` mode -- :func:`~waveflow.rfenv.cache.load_or_trace` at ``channel``,
      with ``retrace`` (``"never"`` for a committed file that must not be rewritten);
    * ``channel`` alone -- a channel file, loaded.

    A relative ``channel`` resolves against ``channel_root`` (or the process's working directory).
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
    if mode == "live":
        if spec is None:
            raise ValueError(f"{who}: live mode needs a spec to trace")
        if channel is not None:
            raise ValueError(
                f"{who}: live mode traces at pre_sim and reads no channel file; drop 'channel', "
                f"or use cached mode to keep the trace there"
            )
        from waveflow.rfenv.sionna_backend import trace

        return trace(spec)
    stem = None
    if channel is not None:
        stem = Path(channel)
        if channel_root is not None and not stem.is_absolute():
            stem = Path(channel_root) / stem
    if spec is not None:
        if stem is None:
            raise ValueError(
                f"{who}: a spec in cached mode needs a channel file stem to cache it in"
            )
        from waveflow.rfenv.cache import load_or_trace

        return load_or_trace(spec, stem, retrace=retrace)
    if stem is None:
        raise ValueError(
            f"{who}: no channel: give a pathset, a channel file, or a spec"
        )
    return PathSet.load(stem)


def _noise(node, samp_rate: float) -> ThermalNoise | None:
    if node.noise_figure_db is None:
        return None
    p = thermal_noise_power(
        float(samp_rate), float(node.noise_figure_db), float(node.temperature_k)
    )
    return ThermalNoise(p, seed=int(node.seed), tag=stream_tag(node.name))


@dataclass
class RtChannel(HwModule):
    """The ray-traced (or any :class:`PathSet`) channel between two RF edges.

    Endpoints ``rf_in`` (this node receives the transmitter's edge) and ``rf_out`` (it produces the
    receiver's edge).  ``rf_in.n_ch`` is the number of transmit channels of the channel, and
    ``rf_out.n_ch`` the number of receive channels; both edges complex, at one sample rate and one
    ``blksize``.  Pysim only: in an XSI graph it has no model and the generator says so.
    """

    #: The channel, handed over directly.
    pathset: PathSet | None = None
    #: A channel file stem (``<stem>.npz`` + ``.json``), or where a cached ``spec`` lives.
    channel: str | Path | None = None
    #: A scene specification: traced into ``channel`` (cached) or at ``pre_sim`` (live).
    spec: SceneSpec | None = None
    #: ``"cached"`` or ``"live"``.
    mode: str = "cached"
    #: Cached mode with a spec: ``"if_stale"``, ``"never"`` (a committed file) or ``"always"``.
    retrace: str = "if_stale"
    #: Anchor for a relative ``channel`` (not a bundle's: that is ``RfDataSource.root``).
    channel_root: Path | None = None
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
        #: Input blocks that never reached this node, replaced by zero input (see the module doc).
        self.dropped_blocks = 0
        #: The channel applied, set in ``pre_sim``.
        self.paths: PathSet | None = None
        self._applier: ChannelApplier | None = None
        self._noise: ThermalNoise | None = None
        self._next_idx = 1

    @property
    def latency_samp(self) -> int:
        """D: samples every path is delayed beyond its own delay, ``filt_len / 2``."""
        return int(self.filt_len) // 2

    def pre_sim(self) -> None:
        who = f"RtChannel '{self.name}'"
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
        self.paths = resolve_channel(
            pathset=self.pathset,
            channel=self.channel,
            spec=self.spec,
            mode=self.mode,
            channel_root=self.channel_root,
            who=who,
            retrace=self.retrace,
        )
        if int(i.n_ch) != self.paths.n_tx_ch or int(o.n_ch) != self.paths.n_rx_ch:
            raise ValueError(
                f"{who}: the channel is {self.paths.n_rx_ch} rx x {self.paths.n_tx_ch} tx, but "
                f"rf_in carries {i.n_ch} and rf_out {o.n_ch} channels"
            )
        self._applier = ChannelApplier(
            self.paths,
            float(i.samp_rate),
            filt_len=int(self.filt_len),
            beta=float(self.beta),
            delay_mode=self.delay_mode,
            start_n0=0,
        )
        self._noise = _noise(self, float(i.samp_rate))
        self._next_idx = 1

    def step(self, idx: int, data: np.ndarray) -> list[np.ndarray]:
        """The output blocks for an input block at grid index ``idx``: one per grid period from the
        last block seen, the missing ones computed from zero input.  ``run_proc``'s body, without
        the edges, so a gap can be exercised directly."""
        blksize = int(np.asarray(data).shape[1])
        g_in, g_out = amplitude_gain(self.tx_gain_db), amplitude_gain(self.gain_db)
        out = []
        for k in range(self._next_idx, int(idx) + 1):
            x = (
                data
                if k == int(idx)
                else np.zeros_like(np.asarray(data, dtype=np.complex128))
            )
            if k != int(idx):
                self.dropped_blocks += 1
            n0 = (k - 1) * blksize
            y = self._applier.process(g_in * np.asarray(x), n0)
            if self._noise is not None:
                y = y + self._noise.samples(y.shape[0], n0, y.shape[1])
            out.append(g_out * y)
        self._next_idx = int(idx) + 1
        return out

    def run_proc(self) -> ProcessGen[None]:
        while True:
            blk = yield from self.rf_in.get()
            self.n_in += 1
            for y in self.step(int(blk.idx), blk.data):
                yield from self.rf_out.put(y)
                self.n_out += 1

    def assert_ran(self, *, where: str = "") -> None:
        """The channel carried blocks, and nothing is stuck inside it (as ``RfSampDelay``)."""
        if not self.n_in:
            raise AssertionError(
                f"{where}RtChannel '{self.name}' was never handed a block"
            )
        if not 0 <= self.n_in + self.dropped_blocks - self.n_out <= 1:
            raise AssertionError(
                f"{where}RtChannel '{self.name}' took {self.n_in} block(s) (and filled "
                f"{self.dropped_blocks}) but emitted {self.n_out}; at most one may be in flight "
                f"when the horizon closes"
            )


@dataclass
class RtSignalSource(HwModule):
    """A waveform through a channel, at a power, with noise: what the receiving antennas deliver.

    Endpoint ``rf_ep`` (the producer of a complex edge whose ``n_ch`` is the channel's receive
    channel count).  It computes block ``k`` from absolute sample ``(k - 1) * blksize`` on: the
    waveform at that index, scaled to ``tx_power_dbm`` (per transmit channel), through the channel,
    plus thermal noise, times ``gain_db``.  The same computation, offline, is
    :meth:`render_bundle`, so pysim and the XSI replay of the bundle carry the same samples.

    **Its blocks are numbered by count**, which is the edge's grid index because a source cannot be
    late: it starts at time zero and runs ahead of the metronome by the edge's buffer, so the edge
    never zero-fills in front of it.

    **Only** ``in_bundle`` **is a** :class:`~waveflow.hw.hw_module.DynParam`: the XSI generator
    emits every non-default ``DynParam`` as a member assignment on the C++ model, and
    ``RfFileSource`` has that one field (``plans/sionna_rt/sionna_rt.md``, Rule 13).
    """

    #: The rendered bundle the XSI twin replays (:meth:`render_bundle` writes it).
    in_bundle: DynParam[str] = ""
    #: Anchor for a relative ``in_bundle``, as ``RfDataSource.root`` (the XSI scenario's).
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
    retrace: str = "if_stale"
    #: Anchor for a relative ``channel``.
    channel_root: Path | None = None
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
        """D: samples every path is delayed beyond its own delay, ``filt_len / 2``."""
        return int(self.filt_len) // 2

    def _resolve(self) -> PathSet:
        if self.paths is None:
            self.paths = resolve_channel(
                pathset=self.pathset,
                channel=self.channel,
                spec=self.spec,
                mode=self.mode,
                channel_root=self.channel_root,
                who=f"RtSignalSource '{self.name}'",
                retrace=self.retrace,
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
        noise = _noise(self, float(samp_rate))
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
        ep = self.rf_ep
        if not ep.complex_samp:
            raise ValueError(
                f"RtSignalSource '{self.name}': the edge must be complex baseband"
            )
        ps = self._resolve()
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
        self,
        bundle_dir: str | Path,
        n_blk: int | None = None,
        *,
        blksize: int | None = None,
        samp_rate: float | None = None,
    ) -> Path:
        """Write the first ``n_blk`` blocks (default :attr:`n_blk`) as a complex RF bundle -- what the
        XSI twin replays.

        The geometry is the bound edge's: a bundle rendered at one rate and replayed on an edge at
        another would carry the wrong delays and Doppler, and the C++ replay checks only the block's
        size.  ``blksize`` and ``samp_rate`` are for an unbound source, and must agree with the edge
        when there is one.
        """
        from waveflow.simulation.rf_tb import write_rf_bundle

        if self.rf_ep.interface is not None:
            edge = (int(self.rf_ep.blksize), float(self.rf_ep.samp_rate))
            asked = (blksize if blksize is not None else edge[0], samp_rate or edge[1])
            if (int(asked[0]), float(asked[1])) != edge:
                raise ValueError(
                    f"RtSignalSource '{self.name}': render at {asked} but the edge is {edge} "
                    f"(blksize, samp_rate); render the geometry the replay will run at"
                )
            blksize, samp_rate = edge
        if blksize is None or samp_rate is None:
            raise ValueError(
                f"RtSignalSource '{self.name}': an unbound source needs blksize and samp_rate"
            )
        n = self.n_blk if n_blk is None else n_blk
        if n is None:
            raise ValueError(
                f"RtSignalSource '{self.name}': how many blocks? set n_blk"
            )
        gen = self.blocks(int(blksize), float(samp_rate))
        blocks = [next(gen) for _ in range(int(n))]
        return write_rf_bundle(blocks, bundle_dir, complex_samp=True)

    def bfm_model(self):
        """XSI twin: ``RfFileSource`` replaying ``in_bundle``, sized as ``RfDataSource`` sizes it.

        One block in **components** (a complex sample is two doubles) and the element kind, which
        the C++ model checks against the bundle's manifest.  An empty ``in_bundle`` would emit no
        assignment and replay nothing, and a real edge cannot carry this source: both refused.
        """
        from waveflow.build.composite_gen import BfmModel

        if not self.in_bundle:
            raise ValueError(
                f"RtSignalSource '{self.name}': set in_bundle (and render it) before generating "
                f"its XSI model"
            )
        if not self.rf_ep.complex_samp:
            raise ValueError(
                f"RtSignalSource '{self.name}': the edge must be complex baseband"
            )
        comps = int(self.rf_ep.n_ch) * int(self.rf_ep.blksize) * 2
        return BfmModel("RfFileSource", ports=("rf_ep",), extra_args=(str(comps), "1"))
