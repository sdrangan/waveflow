"""rfdc.py — :class:`Rfdc`, a model of an RF data converter presenting the real IP's interfaces.

Named ``Rfdc``, not ``RFDCEmulator``: "emulator" describes only one of its three realizations.  In
pysim it is a behavioural model; in XSI (stage 2) it becomes a BFM beside the generated top; in a
bitstream build it is replaced by the real AMD RFDC IP — and **the digital logic must not have to
change its interface** across any of those.

**One module carrying both directions**, not separate ADC / DAC blocks.  The reason is
synchronization: the TX and RX sample counters must hold a fixed relation, and that is a property
*of the converter*, not of two unrelated blocks.  It is also what lets :attr:`t0` have exactly one
owner (see :meth:`on_rf_bind`).

**Reactive on the RF side.**  The metronome lives in
:class:`~waveflow.hw.rf_sample_if.RFSampIF`, so this module has no timer of its own: it responds to
block arrivals on the ADC path and to word arrivals on the DAC path.

**A tile, not a converter.**  One ``Rfdc`` represents ``n_rx`` receive and ``n_tx`` transmit
datapaths, and the two sides take the shape their consumer wants (``plans/adc_model.md``, *The
Waveflow Rfdc is a tile*):

- **RF side** — *one* :class:`~waveflow.hw.rf_sample_if.RFSampIF` per direction, carrying every
  channel of that direction in one ``(n_ch, blksize)`` block.  Splitting it per channel would give
  ``n_ch`` events per block period against the whole point of block-LT.
- **AXIS side** — ``n_rx`` master ports and ``n_tx`` slave ports, one per channel, because that is
  what the IP presents and one port per stream is what keeps the DUT's ports identical across all
  three realizations.  A single wide interleaved port was rejected: it would move a vendor packing
  rule into every design that touches a converter.

``pack`` / ``unpack`` already speak that shape — they return ``(n_ch, n_words)``, so **row ``ch`` is
what port ``ch`` carries** and a channel-major array *is* a per-port array.

Real-valued samples only: ``iq_mode = 1`` is still refused, and the refusal names its two blockers.
Complex-ness is a property of the **word**, never of the port count — see
:class:`~waveflow.hw.rfdc_samp_word.RfdcSampWord`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from waveflow.hw.arrayutils import array
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.rf_sample_if import RFSampIFRx, RFSampIFTx
from waveflow.hw.rfdc_samp_word import RfdcSampWord, pack, unpack
from waveflow.simulation.simobj import ProcessGen


@dataclass
class Rfdc(HwModule):
    """An RF data converter: AXI-Stream to and from the programmable logic, RF sample blocks to and
    from the environment.

    Endpoints
    ---------
    ``rx_rf`` / ``rx_stream_0 .. rx_stream_{n_rx-1}``
        The **ADC** path — one RF interface carrying every receive channel's block, and one AXIS
        master port per channel out to the PL.
    ``tx_stream_0 .. tx_stream_{n_tx-1}`` / ``tx_rf``
        The **DAC** path — one AXIS slave port per channel in from the PL, one RF interface out to
        the environment.

    :attr:`rx_streams` and :attr:`tx_streams` are the same objects as a list, in channel order; the
    indexed attributes exist because ``BfmModel.ports`` names endpoints by **attribute**, and a
    subscript is not an attribute name.

    **Indexed even at one channel**, deliberately.  ``rx_stream`` (unsuffixed) would be a second
    spelling that exists only at ``n_ch == 1``, so every consumer would carry the special case and
    the one-channel path would be the only one anybody tested.  One name, one shape.

    The AXIS endpoints **cross the cut** and take BFM duals; the two RF endpoints do not cross it, so
    they need no dual — but they exist in both backends, which is what ``plans/behavioral_edges.md``
    is for.
    """

    # -- build-time structure: what the synthesized logic is built against -----------------------
    #
    # These count **RF channels, and AXIS ports, because they are the same number** — in both
    # ``iq_mode`` settings.  ``iq_mode`` changes what a word holds, never how many ports there are
    # (``plans/adc_model.md``, *The model carries complex-ness as a type*), so neither name ever
    # comes to mean two things and there is no derived port count to keep in step.
    #
    #: RF receive (ADC) channels — and the number of AXIS master ports.  ``0`` is a real
    #: configuration: a playout design has no receiver (see :meth:`_active_paths`).
    n_rx: HwParam[int] = 1
    #: RF transmit (DAC) channels — and the number of AXIS slave ports.
    n_tx: HwParam[int] = 1

    # -- the packing convention, as one type -----------------------------------------------------
    #: **The AXIS word layout**, and the only place this converter's sample geometry is stated.
    #:
    #: It replaces the three loose parameters ``nbits`` / ``samp_per_word`` / ``iq_mode`` that used
    #: to live here, and there is deliberately **no convenience path back**: keeping either name
    #: beside the type would be a second source of truth for the same geometry, which is exactly how
    #: ``nbits`` came to mean both the converter's resolution *and* its slot width.  A design that
    #: wants a different geometry specializes a word type — ``Rfsoc4x2SampWord.specialize(
    #: samp_per_word=4)`` for this project's board — and the converter reads everything off it.
    #:
    #: A plain field rather than a :class:`~waveflow.hw.hw_module.HwParam`, and that is a mechanical
    #: fact rather than a judgement: ``HwModule.__post_init__`` wraps every ``HwParam`` value in
    #: ``HwParamValue(int(value))``, so a *type*-valued parameter cannot be one.  Nothing is lost —
    #: an ``Rfdc`` declares no ``kernel_task``, so none of its parameters ever reached a template
    #: argument; they were build-time structure for the **models**, which read them here.
    word: type[RfdcSampWord] = RfdcSampWord.specialize(samp_per_word=4)

    # -- init-time knobs -------------------------------------------------------------------------
    #: The amplitude reference quantization is relative to: a sample of ``+full_scale`` maps to the
    #: top of the converter's range.  An **init-time** knob — one artifact serves every value — but
    #: deliberately *not* a :class:`~waveflow.hw.hw_module.DynParam`, and the distinction is finer
    #: than the plan's table assumed.
    #:
    #: ``DynParam`` does not mean "binds at init"; it means "**emitted as a member assignment**"
    #: (``<model>.<field> = <expr>;``).  This value's C++ realization is a *constructor argument* —
    #: it rides inside the ``RfdcFormat`` literal the models take — so tagging it would emit a line
    #: assigning a member that does not exist, which is the obligation recorded in
    #: ``plans/adc_model.md``: a DynParam must land on a real C++ member and nothing static checks
    #: that it does.  Stated once, in :meth:`_fmt_literal`, and read from there.
    #:
    #: (Zero would be doubly wrong — meaningless as an amplitude reference *and* falsy, which
    #: ``discover_dyn_params`` skips — so ``__post_init__`` refuses it either way.)
    full_scale: float = 1.0

    # -- t0: owned here, pushed to the interfaces at bind ----------------------------------------
    #
    # **One epoch per tile, both owned here.**  ADC and DAC are separate tiles on an RFSoC — they run
    # at different sample rates and are started separately — so a single shared number would be a
    # fiction.  What matters, and what the plan's "t0 is the synchronization primitive" argument
    # actually rests on, is that the two epochs have **one owner**: their difference is then a fixed,
    # known quantity of the converter rather than something emergent from scheduling coincidence.  On
    # hardware that difference is exactly what MTS bring-up measures; in simulation it is whatever the
    # design sets, and alignment (``n_rx / fs_rx == n_tx / fs_tx``) is derived from it and assertable.
    #
    #: When the **ADC** tile's sample counter starts.
    t0_rx: float = 0.0
    #: When the **DAC** tile's sample counter starts.  Normally **equal** to :attr:`t0_rx` — that is
    #: what MTS gives you, and it is the default.  A loopback does *not* stagger this to buy pipeline
    #: latency: the one-block cost of a loop through the RF grids is structural and is paid by the
    #: startup transient (see :attr:`~examples.rf_loopback.rf_loopback.RfSampPassThrough.blk_latency`).
    #: Set it non-zero only to model a tile deliberately started late, or a measured MTS residual.
    t0_tx: float = 0.0

    # A converter declares no ``kernel_task()`` — there is no RTL for it, and ``check`` says so
    # through the base's empty ``potential_targets`` rather than through a restatement here.  It DOES
    # declare ``bfm_model()``: one model per data path, each spanning the cut (see below).

    def __post_init__(self) -> None:
        super().__post_init__()
        # There is deliberately NO n_ch > 1 refusal here, and nothing replaced the one that was.
        # It existed because "how many AXIS ports does n_rx mean?" was open; the answer -- one port
        # per channel, complex-ness carried by the WORD -- makes n_rx a single number with a single
        # meaning, so there is no mode/port-count agreement left to check.  The iq_mode refusal below
        # is a different question and stays.
        if int(self.n_rx) < 0 or int(self.n_tx) < 0:
            raise ValueError(
                f"Rfdc: channel counts are non-negative (got n_rx={int(self.n_rx)}, "
                f"n_tx={int(self.n_tx)}).")
        if int(self.n_rx) < 1 and int(self.n_tx) < 1:
            raise ValueError("Rfdc: a tile with neither an ADC nor a DAC path converts nothing.")
        # ZERO is a real configuration, and distinct from the >1 question above: an RX capture design
        # has no transmitter (plans/adc_model.md staging item 3), and wiring a fake DAC into its graph
        # to satisfy this model would put a metronome in the design that nothing feeds — inventing
        # underruns to report.  A path with zero channels is simply absent: no rate check (pre_sim),
        # no process (run_proc), no BFM model (bfm_model).  Its endpoints still EXIST, unbound, which
        # costs nothing and keeps the endpoint set a property of the class rather than of a build.
        if not (isinstance(self.word, type) and issubclass(self.word, RfdcSampWord)):
            raise TypeError(
                f"Rfdc.word must be an RfdcSampWord subclass — the packing convention as a type, "
                f"not a width. Got {self.word!r}. Build one with RfdcSampWord.specialize(...) or a "
                f"board preset such as Rfsoc4x2SampWord.specialize(samp_per_word=4).")
        if self.word.iq_mode:
            raise NotImplementedError(
                f"Rfdc implements real samples only (iq_mode=0), got a word declaring "
                f"interleaved I/Q ({self.word.describe()}). Interleaved I/Q doubles the bits per "
                f"sample slot and needs two things this converter has not got: the COMPLEX RF "
                f"bundle format and a conformance twin that covers complex (plans/adc_model.md "
                f"stages B and C; D is this refusal). The "
                f"WORD type can already express it — iq_order is declared and tested there — so "
                f"what is missing is the converter's two halves, not the packing rule.")
        if not self.full_scale or float(self.full_scale) <= 0:
            raise ValueError(
                f"Rfdc.full_scale must be a positive amplitude reference, got {self.full_scale!r}. "
                f"(0.0 is doubly wrong here: it is meaningless *and* falsy, so discover_dyn_params "
                f"would skip it and the generated model would silently take its C++ default.)")
        w = self.axis_bitwidth
        if w > 64:
            raise ValueError(
                f"Rfdc: the AXIS word is {self.word.describe()}, wider than the 64-bit stream word.")

        #: The element type one sample is quantized to — read off the word type, which is the only
        #: place the converter's *effective* resolution is stated.  ``ap_fixed<bits_per_samp, 1>``
        #: over [-1, 1), rounding and **saturating** (a converter clips, it does not wrap), and
        #: integer-backed so it is bit-exact with the Vitis type rather than a float approximation
        #: of it.  It is deliberately NOT derived from the word width: a 14-bit converter on a
        #: 16-bit bus quantizes to 14.
        self.SampType = self.word.samp_type()

        # One RF endpoint per direction, carrying every channel of that direction; one AXIS endpoint
        # per channel.  The asymmetry is the point -- see the class docstring.
        self.rx_rf = RFSampIFRx(sim=self.sim, name=f"{self.name}_rx_rf")
        self.tx_rf = RFSampIFTx(sim=self.sim, name=f"{self.name}_tx_rf")
        #: The ADC path's AXIS master ports, in channel order.  Row ``ch`` of what :func:`pack`
        #: returns is what ``rx_streams[ch]`` carries.
        self.rx_streams = [
            StreamIFMaster(sim=self.sim, name=f"{self.name}_rx_stream_{i}", bitwidth=w,
                           has_tlast=True)
            for i in range(int(self.n_rx))]
        #: The DAC path's AXIS slave ports, in channel order.
        self.tx_streams = [
            StreamIFSlave(sim=self.sim, name=f"{self.name}_tx_stream_{i}", bitwidth=w,
                          has_tlast=True)
            for i in range(int(self.n_tx))]
        # The indexed ATTRIBUTES are not a convenience: `BfmModel.ports` names a model's constructor
        # arguments by attribute name (`getattr(part, attr)`), and `rx_streams[0]` is not one.  So the
        # list and the attributes are two views of the same objects, and both are registered once.
        for i, ep in enumerate(self.rx_streams):
            setattr(self, f"rx_stream_{i}", ep)
        for i, ep in enumerate(self.tx_streams):
            setattr(self, f"tx_stream_{i}", ep)
        for ep in (self.rx_rf, *self.rx_streams, *self.tx_streams, self.tx_rf):
            self.add_endpoint(ep)

        # Read off the RF interfaces at bind (see on_rf_bind); None until then.
        self.rx_samp_rate: float | None = None
        self.tx_samp_rate: float | None = None
        self.rx_blksize: int | None = None
        self.tx_blksize: int | None = None
        #: Blocks converted on each path — reporting, not a contract (the contract is the interface
        #: counters).
        self.n_adc_blk = 0
        self.n_dac_blk = 0

    # -- derived structure -----------------------------------------------------------------------

    @property
    def axis_bitwidth(self) -> int:
        """AXIS word width in bits — **read off** :attr:`word`, never restated here.

        The same single-source discipline ``samp_rate`` follows: the quantity is declared once, on
        the object it is a property of, and the converter reads it.  A second expression here could
        disagree with the type the serializers are handed, and the disagreement would be silent.

        The arithmetic lives on the word type: ``samp_per_word * bits_per_samp_pack``, doubled for
        interleaved I/Q, because a complex sample occupies two slots.  So an I/Q design fits the
        same 64-bit bus by **halving** ``samp_per_word``.  See ``docs/guide/rf/rfdc/axis_side.md``.
        """
        return int(self.word.bitwidth)

    # -- bind-time: read the rate, push the epoch ------------------------------------------------

    def on_rf_bind(self, iface, ep_name: str) -> None:
        """Called by :class:`~waveflow.hw.rf_sample_if.RFSampIF` when one of this module's RF
        endpoints is bound.

        Two reads in **opposite directions**, each where the quantity physically belongs:

        - ``samp_rate`` lives on the interface's clock and is **read** here.  A converter declaring
          its own copy would be a second declaration that could disagree — the same single-source
          discipline as ``StreamIF.depth``.
        - ``t0`` is owned **here** and **pushed** onto the interface, because it describes a tile's
          sample counter rather than any one wire.  One source sets it for every interface this
          module binds, which is how the two edges get a *fixed, known* relation without being one
          object (see :attr:`t0_rx` / :attr:`t0_tx`).
        """
        iface.set_t0(self.t0_rx if ep_name == 'rx' else self.t0_tx, owner=self)

        # The one number stated twice, so it is checked once.  `n_ch` on the edge and `n_rx`/`n_tx`
        # here are the SAME count -- the RF block's rows are the AXIS ports -- and a disagreement
        # would show up as a shape error deep inside `pack`, or worse, not at all when the smaller
        # of the two happens to divide the larger.
        want = int(self.n_rx) if ep_name == 'rx' else int(self.n_tx)
        if int(iface.n_ch) != want:
            side = 'n_rx' if ep_name == 'rx' else 'n_tx'
            raise ValueError(
                f"Rfdc '{self.name}': {iface.name} carries {int(iface.n_ch)} channel(s) but this "
                f"tile declares {side}={want}. They are one number -- row ch of the RF block is what "
                f"AXIS port ch carries -- so one of the two is wrong.")

        if ep_name == 'rx':
            self.rx_samp_rate = iface.samp_rate
            self.rx_blksize = int(iface.blksize)
        else:
            self.tx_samp_rate = iface.samp_rate
            self.tx_blksize = int(iface.blksize)

    def _active_paths(self):
        """The converter paths this tile actually has: ``[(name, samp_rate, axis endpoints), ...]``.

        The third element is the path's **whole port list**, one entry per channel, because a path is
        the unit every consumer here works in: one rate check per port, one BFM model per path.

        **A tile may be ADC-only or DAC-only**, and that is a real configuration rather than a
        convenience: an RX capture design (``plans/adc_model.md`` staging item 3) has no transmitter,
        and wiring a fake DAC into its graph purely to satisfy this model would put a metronome in
        the design that nothing feeds — inventing underruns to report.

        ``n_rx`` / ``n_tx`` already declare the channel counts, so zero is the natural way to say
        "this path does not exist"; nothing new is declared for it.  Every path a tile *does* have is
        checked exactly as before, so a two-path tile is unaffected.
        """
        paths = []
        if int(self.n_rx) > 0:
            paths.append(('ADC', self.rx_samp_rate, self.rx_streams))
        if int(self.n_tx) > 0:
            paths.append(('DAC', self.tx_samp_rate, self.tx_streams))
        return paths

    def pre_sim(self) -> None:
        """Check the one rate relation the AXIS port physically cannot violate.

        ``samp_rate <= samp_per_word * f_axis`` — a ratio above 1 means more samples arrive per
        second than the port can carry, which is a design error, not something to simulate.  Fail
        loud.  (The *fractional* part of that ratio is the credit accumulator the C++ model needs;
        the Python model needs neither conversion — it works in seconds.)
        """
        for path, rate, eps in self._active_paths():
            if rate is None:
                raise RuntimeError(
                    f"Rfdc '{self.name}': the {path} RF endpoint was never bound to an RFSampIF, so "
                    f"there is no sample rate to check against the AXIS clock.")
            # PER PORT, not once for the path: each channel has its own AXIS interface and nothing
            # forces them onto one clock, so a rate that fits channel 0's fabric says nothing about
            # channel 1's.  One check each is the only honest reading.
            for ch, ep in enumerate(eps):
                if ep.interface is None or ep.interface.clk is None:
                    raise RuntimeError(
                        f"Rfdc '{self.name}': the {path} AXIS endpoint for channel {ch} "
                        f"({ep.name}) has no bound interface/clock.")
                f_axis = float(ep.interface.clk.freq)
                cap = int(self.word.samp_per_word) * f_axis
                if rate > cap:
                    raise ValueError(
                        f"Rfdc '{self.name}': {path} channel {ch} sample rate {rate:g} Hz exceeds "
                        f"what the AXIS port can carry — samp_per_word * f_axis = "
                        f"{int(self.word.samp_per_word)} * {f_axis:g} = {cap:g} samples/s. Raise "
                        f"samp_per_word, widen the port, or lower the rate.")
        if int(self.n_rx) > 0 and int(self.rx_blksize) % int(self.word.samp_per_word):
            raise ValueError(
                f"Rfdc '{self.name}': ADC blksize {int(self.rx_blksize)} is not a multiple of "
                f"samp_per_word {int(self.word.samp_per_word)}; a sample cannot straddle a word.")
        if int(self.n_tx) > 0 and int(self.tx_blksize) % int(self.word.samp_per_word):
            raise ValueError(
                f"Rfdc '{self.name}': DAC blksize {int(self.tx_blksize)} is not a multiple of "
                f"samp_per_word {int(self.word.samp_per_word)}.")

    # -- the XSI realization ---------------------------------------------------------------------

    def f_axis(self, ep) -> float:
        """The fabric clock, **read** off the AXIS interface *ep* is bound to.

        The same single-source discipline ``samp_rate`` already follows: the frequency is declared
        once, on the clock of the interface that physically carries it, and the converter reads it
        rather than restating it.  Read here at *use* rather than cached at bind because
        :meth:`bfm_model` is the only consumer and it runs on a fully-bound graph — caching it would
        add a second copy with nothing to gain.
        """
        if ep.interface is None or ep.interface.clk is None:
            raise RuntimeError(
                f"Rfdc '{self.name}': {ep.name} has no bound interface/clock, so the fabric rate "
                f"the model needs cannot be read. Bind the AXIS side before generating.")
        return float(ep.interface.clk.freq)

    def words_per_cycle(self, ep, samp_rate: float) -> float:
        """AXIS words per fabric cycle — ``samp_rate / (samp_per_word * f_axis)``.

        **Derived, never declared.**  Both terms already exist elsewhere (the RF interface's clock,
        the AXIS interface's clock), so a declared ratio would be a third statement of something the
        design fixes twice — and the one that could disagree.  Fractional by nature: 256/(4*300) MHz
        is 0.2133, which no integer expresses, and it is the ``RateTick`` accumulator's input.
        """
        return float(samp_rate) / (int(self.word.samp_per_word) * self.f_axis(ep))

    def _fmt_literal(self) -> str:
        """``RfdcFormat{bits_per_samp, samp_per_word, full_scale, bits_per_samp_pack,
        justify_shift}`` as a C++ **literal**.

        Five fields, and the last two are the effective/container split reaching the twin: a
        ``RfdcFormat`` that carried one width could only model a part whose resolution equals its
        slot width.  ``RfdcFormat`` is **aggregate-initialized** from this string, so the order here
        is the struct's declaration order and the two must not drift — the appended-not-interleaved
        shape in ``xsi_rfdc_samp.h`` is what keeps that cheap to check.

        A literal and not an identifier, deliberately: the harness promotes any bare identifier in
        ``extra_args`` to a ``Harness(...)`` parameter typed ``const std::vector<uint64_t>&``, and an
        ``RfdcFormat`` is not that.  Recorded as a trap in ``plans/adc_model.md`` before it could
        bite; this is the shape that avoids it with no generator change.
        """
        return (f"RfdcFormat{{{int(self.word.bits_per_samp)}, {int(self.word.samp_per_word)}, "
                f"{float(self.full_scale)!r}, {int(self.word.bits_per_samp_pack)}, "
                f"{int(self.word.justify_shift())}}}")

    def bfm_model(self):
        """**Two** models, one per data path, each spanning the cut — and each spanning *every* AXIS
        port of its direction.

        The ADC path is one object binding RTL pins on the fabric side and the RF channel on the
        other — which is what a converter *is*, rather than a boundary model glued to a separate
        channel peer.  Port order is constructor order, and it is ``xsi_rfdc.h``'s:
        ``(Dut&, AxisPortList, RfChannel&, const RfdcFormat&, double [, size_t])``.

        **One model per direction, not one per channel**, and that is forced rather than chosen: the
        RF edge carries every channel in one block, so ``n_ch`` independent models cannot each own
        it.  The AXIS ports of a direction are therefore a **group** — a single ``BfmModel.ports``
        entry that is a tuple — which resolves to one ``AxisPortList`` constructor argument.  At one
        channel that renders exactly as it always did (a bare port name), so a one-channel design's
        generated harness is unchanged.

        This is the first real consumer of per-port ``BfmModel`` resolution
        (``plans/adc_model.md``): the AXIS group resolves to ``sim.dut(), <ns>::<port>...`` because
        its peers are DUT boundary ports, and ``rx_rf`` to a channel variable because its peer is
        not.
        """
        from waveflow.build.composite_gen import BfmModel

        for name, rate, _eps in self._active_paths():
            if rate is None:
                raise RuntimeError(
                    f"Rfdc '{self.name}': the {name} RF endpoint was never bound, so the sample rate "
                    f"the models need has not been read. Bind every RF interface this tile declares "
                    f"before generating.")
        models = []
        if int(self.n_rx) > 0:
            adc_rate = self.words_per_cycle(self.rx_streams[0], self.rx_samp_rate)
            group = tuple(f"rx_stream_{i}" for i in range(int(self.n_rx)))
            models.append(BfmModel("RfdcAdcMaster", ports=(group, "rx_rf"),
                                   extra_args=(self._fmt_literal(), repr(adc_rate))))
        if int(self.n_tx) > 0:
            dac_rate = self.words_per_cycle(self.tx_streams[0], self.tx_samp_rate)
            # blk_samples is n_ch * blksize -- one block's worth, the unit the RF edge moves.  The
            # DAC needs it to know when a block is complete; the ADC learns it from the block it is
            # handed.
            #
            # Read off tx_rf, not rx_rf.  It used to read the RX edge, which is the same number
            # whenever both paths exist -- and unbound when they do not.  A DAC-ONLY tile (n_rx=0,
            # the natural shape for a playout design) therefore could not be lowered at all: the
            # accessor raised "not bound to an RFSampIF".  The DAC path's channel count is a property
            # of the DAC's own edge, so that is where it comes from.
            blk_samples = int(self.tx_rf.n_ch) * int(self.tx_blksize)
            group = tuple(f"tx_stream_{i}" for i in range(int(self.n_tx)))
            models.append(BfmModel("RfdcDacSlave", ports=(group, "tx_rf"),
                                   extra_args=(self._fmt_literal(), repr(dac_rate),
                                               str(blk_samples))))
        return tuple(models)

    # -- the two conversion paths ----------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None]:
        """Run both directions concurrently: the DAC path as its own process, the ADC path here.

        A tile configured ``n_tx = 0`` (or ``n_rx = 0``) runs one path only — see
        :meth:`_active_paths`."""
        if int(self.n_tx) > 0:
            self.process(self._dac_proc())
        if int(self.n_rx) > 0:
            yield from self._adc_proc()

    def _adc_proc(self) -> ProcessGen[None]:
        """RF block in → quantize → pack → one AXIS burst out.

        Quantization goes through the integer-backed ``FixedField``, and packing through the
        generated array (de)serializers — never a hand-rolled ``.range()``.  That is what makes "evaluate
        the effect of bit widths in Python" mean the same thing the RTL will do.

        **``offer``, not ``write``, and at the CONVERTER's rate.**  Two corrections, and pysim needed
        both before it could see what the RTL sees:

        * ``write()`` waits for room.  An ADC cannot: it presents a beat every sample period and what
          the fabric does not take is gone.  ``offer()`` takes what fits and the interface counts the
          rest.
        * The burst must be charged at ``samp_rate / samp_per_word`` words per second, not at the
          fabric clock.  A block of 64 words is 1000 ns of converter output; charging it at 300 MHz
          claimed 213 ns and handed the consumer a 787 ns hole to drain in that the hardware never
          gives it.  Being 4.7x too fast is exactly what hid the loss.
        """
        fs = float(self.full_scale)
        word_rate = float(self.rx_samp_rate) / int(self.word.samp_per_word)
        while True:
            blk = yield from self.rx_rf.get()
            samples = np.asarray(blk.data, dtype=np.float64)        # (n_rx, blksize)
            self.n_adc_blk += 1
            words = self._pack(samples / fs)                        # (n_rx, n_words)
            # CONCURRENTLY, one process per port.  Offering the rows in a `for` loop would put the
            # channels end to end in time -- channel 1 starting only after channel 0's whole block
            # had gone out -- which is not what n_ch converters wired to n_ch ports do, and would
            # invent a rate violation on every channel but the first.
            sends = [self.env.process(ep.offer(words[ch], word_rate=word_rate))
                     for ch, ep in enumerate(self.rx_streams)]
            yield self.env.all_of(sends)

    def _dac_proc(self) -> ProcessGen[None]:
        """One AXIS burst in → unpack → dequantize → RF block out.

        The burst is taken as **raw words**, which is what a DAC is handed: whatever
        :meth:`_unpack` needs to know about their layout it reads off :attr:`word`, and asking the
        stream to deserialize slots for us would have made the pair asymmetric — ``_pack`` producing
        words while ``_unpack`` consumed slots is precisely how the two stopped being inverses.
        """
        fs = float(self.full_scale)
        n_words = int(self.tx_blksize) // int(self.word.samp_per_word)
        while True:
            # Concurrently, for the mirror of the reason the ADC path fans out: the n_tx ports are
            # fed by n_tx independent producers, and reading them one after another would make each
            # channel wait for its predecessor's whole block.
            gets = [self.env.process(ep.get(nwords_max=n_words)) for ep in self.tx_streams]
            yield self.env.all_of(gets)
            rows = [np.asarray(getattr(g.value, "val", g.value)).reshape(-1) for g in gets]
            short = [(ch, r.size) for ch, r in enumerate(rows) if r.size != n_words]
            if short:
                raise RuntimeError(
                    f"Rfdc '{self.name}': DAC channel(s) {short} delivered a partial block; every "
                    f"port carries {n_words} words per block period. A converter's channels are one "
                    f"tile, so they cannot be assembled out of bursts of different lengths.")
            self.n_dac_blk += 1
            yield from self.tx_rf.put(self._unpack(np.stack(rows)) * fs)

    # -- quantize, then lay out -- and the inverse ------------------------------------------------
    #
    # TWO steps, and they are different questions.  Quantization is the CONVERTER's, at
    # ``bits_per_samp``; the slot layout is the BUS's, at ``bits_per_samp_pack``.  Only the first is
    # this module's: the second is
    # :func:`~waveflow.hw.rfdc_samp_word.pack` / :func:`~waveflow.hw.rfdc_samp_word.unpack`, which
    # own the justification shift, the slot order and the serializer calls.  What is left here is
    # the amplitude reference, and that is right -- ``full_scale`` is a property of the converter,
    # not of the word.
    #
    # These two stay because the converter works in *normalized reals* rather than stored integers.
    # They are the quantizer and nothing else: there is one implementation of the packing and it is
    # not here.
    #
    # Both work on the WHOLE (n_ch, .) array in one call, which is the shape `pack` / `unpack` were
    # built for.  They used to `reshape(1, -1)` on the way in and index `[0]` on the way out -- a
    # one-channel adapter around a channel-major pair, which is exactly the code a tile does not
    # need.

    def _pack(self, normalized: np.ndarray):
        """Normalized reals in [-1, 1), ``(n_ch, n_samp)`` → AXIS words, ``(n_ch, n_words)``."""
        stored = np.asarray(from_real(normalized, self.SampType), dtype=np.int64)
        return pack(self.word, stored)

    def _unpack(self, samp_words) -> np.ndarray:
        """AXIS words ``(n_ch, n_words)`` → normalized reals — the exact inverse of :meth:`_pack`."""
        return to_real(array(self.SampType, unpack(self.word, np.asarray(samp_words))))
