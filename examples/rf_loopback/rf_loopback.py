"""rf_loopback.py — the stage-1 RF example: source → Rfdc (ADC) → DUT → Rfdc (DAC) → sink.

The ADC arc's ``mem_copy``: deliberately the smallest graph that exercises every structural decision
in ``plans/adc_model.md`` — the component-kind question, the underflow/overflow contract, the
parameter split, the absolute-grid metronome and ``t0`` — before any of them is expensive to change.
There is no DSP here on purpose: the digital logic is a pass-through, so any difference between what
went in and what came out is the *plumbing*, not an algorithm.

Three files, three roles:

- :mod:`waveflow.hw.rf_sample_if` — the edge (framework).
- :mod:`waveflow.simulation.rf_tb` — the RF environment participants (framework).
- here — the converter's neighbours: a trivial digital-logic DUT, the testbench **graph**, and the
  **procedure** that drives it.

The graph/procedure split is the one ``mem_copy`` uses and for the same reason: a component graph is
*data* and can be walked (stage 2 generates an XSI harness from it); a run-and-check function is
*code* and cannot.  So :class:`RfLoopbackTB` builds only structure and :class:`RfLoopbackSim` owns
the scenario, the run, and the golden.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.synth import sim_only
from waveflow.simulation.rf_tb import RfDataSink, RfDataSource, read_rf_bundle, write_rf_bundle
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

from examples.rf_loopback.rfdc import Rfdc


def blk_array(bitwidth: int, nwords: int) -> type:
    """One block's payload type: ``DataArray`` of *nwords* words of *bitwidth*.

    The **instance → type bridge**, as a free function so the two children that need it (the pysim
    twin's burst and the relay's block) name one type rather than two that happen to agree.  Reading
    it through a property inside a synthesizable body is allowed by the implicit-capture rule
    precisely because it resolves to a ``DataSchema`` subclass — a type is a fact about the design,
    not storage someone has to write.
    """
    return DataArray.specialize(
        element_type=IntField.specialize(bitwidth=int(bitwidth), signed=False),
        max_shape=(int(nwords),), static=True)


@dataclass
class RfSampIngress(FreeRunMod):
    """The ingress adapter: one word off the AXIS boundary, one word into the internal FIFO.

    **This exists because a converter cannot be back-pressured.**  The block stage behind it is busy
    for a contiguous stretch of every block period, and while it is, nothing is reading ``s_in`` — so
    the ADC, which presents a beat every ~4.7 cycles regardless, loses whatever the 2-deep boundary
    port cannot hold.  (A top-level AXIS argument *is* 2 deep: a depth pragma on it is ignored by
    Vitis, which is why the elastic buffer has to be a task and an internal channel rather than a
    number in the Python.  See ``docs/guide/rf/fidelity.md``.)

    This task never stops reading: its whole firing is one read and one write, so ``TREADY`` is low
    for at most a cycle at a time and the depth needed to absorb the block stage's busy stretch lives
    in the internal FIFO behind it, where a depth declaration *is* honoured.

    **The body is hand-written, and the reason is a real boundary rather than an extractor gap.**
    ``run_iter`` is the pysim golden twin and it relays a whole *burst*, because that is the only
    granularity pysim has: ``StreamIFSlave.get`` pops one burst and truncates it to the requested
    width, so a word-granular Python body would silently discard 63 of every 64 words.  The word
    relay the hardware needs therefore has no pysim expression, and the two are declared separately —
    identical at block granularity, which is the only granularity pysim resolves.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_ingress"

    #: AXIS word width in bits — the only parameter the hardware body has.  It takes no block size:
    #: a word relay does not know what a block is, and that is the point.
    bitwidth: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.w_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_w_out", bitwidth=w,
                                    has_tlast=True)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.w_out)

    def kernel_task(self) -> KernelTask:
        """The hand-written one-word relay (``rf_samp_ingress_task.h``).

        Overriding this is what "the body is mine, not the generator's" means; nothing can derive a
        function name or a parameter order someone else chose.  See
        ``docs/guide/comp_codegen/freerunning_override.md``.
        """
        return KernelTask("rf_samp_ingress_task", "rf_samp_ingress_task.h", ("s_in", "w_out"),
                          template_args=(int(self.bitwidth),))

    def run_iter(self) -> ProcessGen[None]:
        """The pysim twin: relay one whole burst, because a burst is pysim's quantum.

        The raw-word convention on purpose — this body is not extracted, and a burst-shaped relay
        must not name a block size it does not have.
        """
        words = yield from self.s_in.get()
        yield from self.w_out.write(words)


@dataclass
class RfSampBlockRelay(FreeRunMod):
    """The block stage: collect a whole block off the internal FIFO, then emit it.

    Trivial on purpose — the arc is about the converter boundary, and a pass-through makes the
    loopback golden exact rather than approximately equal.  Stage 3's ``RfSampBuf`` is the first
    block that does something.  What matters structurally is that this stage is *allowed* to be busy:
    it holds a block, and while it writes one out the ingress ahead of it keeps draining the port.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_relay"

    bitwidth: HwParam[int] = 64
    #: Words in one block's burst — ``blksize / samp_per_word``.
    nwords_blk: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.blk_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_blk_in", bitwidth=w,
                                    has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.add_endpoint(self.blk_in)
        self.add_endpoint(self.s_out)
        #: Blocks relayed — the sanity number the check reads.  Incremented through
        #: :meth:`count_burst`, never inline: see there for why.
        self.n_blk = 0

    @sim_only
    def count_burst(self) -> None:
        """Tally one relayed block — **instrumentation, and marked as such.**

        ``self.n_blk += 1`` inline in :meth:`run_iter` is what the extractor's implicit-capture rule
        exists to catch, and it caught it: a read of ``self.X`` in a synthesizable body could be a
        constant baked into the design, a register someone must write, or a counter that means
        nothing in hardware, and nothing can tell those apart. The rule makes the author say which.

        ``@sim_only`` is the "means nothing in hardware" answer, and it has to sit on a **method**:
        the validator tests ``_is_sim_only`` on the resolved object, and an ``int`` cannot carry an
        attribute. ``add_state`` would have been the wrong answer — it declares persistent hardware
        storage, and would put a live counter in the RTL that no design reads.
        """
        self.n_blk += 1

    @property
    def blk_words(self) -> type:
        """The payload type of one firing — see :func:`blk_array`."""
        return blk_array(int(self.bitwidth), int(self.nwords_blk))

    def run_iter(self) -> ProcessGen[None]:
        # The TYPED get, not the raw-word ``get(nwords_max=...)`` form.  That form is documented as
        # the "old (raw-word) calling convention ... used by non-HwModule callers such as PolyTB",
        # and the extractor has no rule for it — it reaches for the schema type that a raw get does
        # not carry.  A synthesizable body uses the typed convention.
        blk = yield from self.blk_in.get(self.blk_words)
        self.count_burst()
        yield from self.s_out.write(blk)


@dataclass
class RfSampPassThrough(FreeRunMod):
    """The digital logic: one AXIS burst in, the same burst out — as **two overlapped tasks**.

    ::

        s_in --> [RfSampIngress] --blk_fifo (depth = nwords_blk)--> [RfSampBlockRelay] --> s_out

    A free-running module, because that is what the fabric between two converter ports is — it has no
    host to start it and re-fires on each arriving block.

    **Why it is not one task.**  It was, and it lost samples.  A single body that reads a whole block
    and only then writes it leaves ``TREADY`` low for the entire write, and an ADC does not wait: 72
    of 512 words were dropped at RTL.  Splitting the read from the write is what lets block *k+1*
    arrive while block *k* is going out, and it is the same shape ``mem_copy`` has — one
    ``hls::task`` per child, wired by an internal channel.

    The FIFO between them carries a **declared depth of one block**, and that declaration is honoured
    because it is an *internal* channel: ``#pragma HLS STREAM depth=`` works there and is ignored on a
    top-level port (``HLS 214-387``).  That asymmetry is not a footnote here — it is the reason the
    elastic buffer has to be a channel inside the top rather than a deeper boundary port.
    """

    #: The generated top's name.  Without it the module has no codegen identity and
    #: ``tb_top_spec`` has no ``top_name`` — which, not the boundary, is what actually kept this
    #: DUT out of a testbench walk.
    cpp_kernel_name: ClassVar[str | None] = "rf_pass_through"

    #: AXIS word width in bits — ``samp_per_word * nbits`` at the converter.  Read off the
    #: :class:`~examples.rf_loopback.rfdc.Rfdc` when the testbench builds the graph.
    bitwidth: HwParam[int] = 64
    #: Words in one block's burst — ``blksize / samp_per_word``.  Also the internal FIFO's depth:
    #: the ingress must be able to hand off a whole block without waiting on the block stage.
    nwords_blk: HwParam[int] = 64
    #: **Block latency**: how many RF block periods pass before this module's output for a given
    #: input block is available downstream.
    #:
    #: It is **>= 1 for any block-processing module, structurally** — not as a safety margin.  A block
    #: only exists at its grid tick, so a module cannot emit block *k* before it has received block
    #: *k*, and a converter downstream therefore cannot play block *k* in the same period the
    #: converter upstream delivered it.  A loop through the RF grids costs at least one block index no
    #: matter how fast the fabric is; even a zero-latency fabric cannot close it.  That is the
    #: "no dependency within < blksize samples" limit in ``plans/adc_model.md``, applied to the fabric
    #: path rather than the environment path.
    #:
    #: **Still one block after the overlap**, and that is the point of the overlap: the two stages
    #: cost ~2 x nwords_blk fabric cycles end to end, which at this rate is a fifth of a block period.
    #: Pipelining the module did not make it deeper in *blocks*; it made it stop stalling its input.
    #:
    #: This is a **declaration that gets checked**, not a knob: the testbench asserts the downstream
    #: edge underran exactly this many times at startup, so a module that claims two blocks and
    #: exhibits one fails.  (Wall-clock latency here is sub-block — the fabric is ~4.7x oversized for
    #: this rate — but a design must not lean on that: sub-block timing is precisely what block-LT
    #: does not resolve.)
    blk_latency: HwParam[int] = 1
    #: The fabric clock the internal channel runs at.  Passed in by the testbench that owns the AXIS
    #: domain, because the hop's cost is a property of the fabric, not of this module.
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        if int(self.blk_latency) < 1:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': blk_latency must be >= 1 block, got "
                f"{int(self.blk_latency)}. A zero-latency loop through the RF grids is not a system "
                f"that can exist — block k cannot be played in the period it was captured — so it is "
                f"refused here rather than reported later as an underrun.")
        w = int(self.bitwidth)
        nw = int(self.nwords_blk)

        # --- sub-components (add_comp; insertion order == codegen task order) ---
        self.ingress = RfSampIngress(name=f"{self.name}_ingress", sim=self.sim, bitwidth=w)
        self.relay = RfSampBlockRelay(name=f"{self.name}_relay", sim=self.sim, bitwidth=w,
                                      nwords_blk=nw)
        for c in (self.ingress, self.relay):
            self.add_comp(c)

        # --- the internal channel: the elastic buffer, one block deep -------------------------
        # Depth is single-source here in the way it is NOT at a boundary port: pysim reads it as the
        # slave's queue_size and codegen emits `#pragma HLS STREAM depth=` for it, because this
        # channel is inside the top.  One block is what the ingress must be able to hand off while
        # the relay is busy writing the previous one.
        self._blk_if = StreamIF(name=f"{self.name}_blk_fifo", sim=self.sim, clk=self.clk,
                                bitwidth=w, depth=nw)
        self._blk_if.bind("master", self.ingress.w_out)
        self._blk_if.bind("slave", self.relay.blk_in)
        self.add_if(self._blk_if)

        # Boundary port NAMES only — the endpoints and their order are the unwired child ports in
        # add_comp x add_endpoint order, and the endpoint's TYPE gives the direction.  The names must
        # be stated because the top's ports keep the names the converters bind to.
        self.boundary = ["s_in", "s_out"]

        # convenience refs for the testbenches (the boundary endpoints live on the children)
        self.s_in = self.ingress.s_in
        self.s_out = self.relay.s_out

    @property
    def n_blk(self) -> int:
        """Blocks relayed end to end — the block stage's count, which is the composite's."""
        return int(self.relay.n_blk)

    @property
    def blk_words(self) -> type:
        """The payload type of one block — see :func:`blk_array`."""
        return blk_array(int(self.bitwidth), int(self.nwords_blk))


@dataclass
class RfLoopbackTB(FreeRunMod):
    """The testbench as a component **graph**: five nodes and four edges, no procedure.

    ::

        RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_stream --StreamIF--> RfSampPassThrough
                                                                                    |
        RfDataSink   <--RFSampIF-- Rfdc.tx_rf | Rfdc.tx_stream <--StreamIF-----------+

    Two of the edges are :class:`~waveflow.hw.rf_sample_if.RFSampIF` (the RF domain, one per
    direction) and two are ``StreamIF`` (the PL domain).  Only the ``StreamIF`` pair would cross the
    cut in stage 2; the RF pair stays behavioural on both sides of it.

    ``n_blk`` is the number of block periods the RF metronomes run.  Setting it larger than the
    number of blocks the source supplies is how underrun is provoked — a testbench knob, not a
    design one.
    """

    #: Blocks the RF grid runs for, on each direction's interface.
    n_blk: int = 8
    #: Samples per channel per block.  The fidelity/speed knob: this is what one SimPy event carries.
    blksize: int = 256
    #: RF sample rate in Hz (lives on the RF interfaces' clock; the converter reads it at bind).
    samp_rate: float = 256e6
    #: AXIS / fabric clock.
    axis_freq: float = 300e6
    nbits: int = 16
    samp_per_word: int = 4
    full_scale: float = 1.0
    #: The tiles' epoch — pushed to **both** RF interfaces at bind, so the ADC and DAC grids are
    #: aligned.  That is what MTS gives you, and it is deliberately *not* where the loop's one-block
    #: cost is paid: staggering a tile start to buy pipeline latency would model something MTS exists
    #: to prevent.  The cost is structural and shows up as the startup transient instead — see
    #: :attr:`RfSampPassThrough.blk_latency`.
    t0: float = 0.0
    #: Seconds the source waits before its first block — a **late** producer (provokes underrun).
    src_start_delay: float = 0.0
    #: Blocks after which the sink stops consuming forever (provokes overrun).  ``None`` = never.
    sink_stall_after: int | None = None
    #: Receiver queue depth at the sink, in blocks.
    sink_depth: int = 2
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))

        #: Seconds per block on both RF grids — ``blksize / samp_rate``.
        self.blk_period = int(self.blksize) / float(self.samp_rate)
        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, nbits=int(self.nbits),
                         samp_per_word=int(self.samp_per_word), full_scale=float(self.full_scale),
                         t0_rx=float(self.t0), t0_tx=float(self.t0))
        w = self.rfdc.axis_bitwidth
        self.nwords_blk = int(self.blksize) // int(self.samp_per_word)

        self.dut = RfSampPassThrough(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                                     nwords_blk=self.nwords_blk, clk=self.axis_clk)
        #: Block latency of the wired path from the ADC edge to the DAC edge — **summed from what the
        #: modules on that path declare**, not inferred.  A general graph would need loop detection;
        #: a design that states its latency does not.  This is the DAC edge's entitled startup
        #: transient.
        #:
        #: Two terms, not one.  The DUT's own hop, plus **one block for the ADC**: a converter cannot
        #: emit samples it has not yet collected, so a block exists only at its grid tick and is
        #: transmitted across the *following* period.  That hop was invisible while the model charged
        #: the ADC's burst at the fabric clock (213 ns instead of 1000 ns) and appeared the moment the
        #: transfer was paced by the converter's own rate.  It is the same quantity the fidelity
        #: contract states as "no dependency shorter than 2*blksize — one block per converter hop".
        self.loop_blk_latency = 1 + int(self.dut.blk_latency)
        self.source = RfDataSource(name=f"{self.name}_src", sim=self.sim, in_bundle="vectors/rf_in",
                                   start_delay=float(self.src_start_delay))
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim, out_bundle="vectors/rf_out",
                               depth=int(self.sink_depth), stall_after=self.sink_stall_after)

        for c in (self.dut, self.rfdc, self.source, self.sink):
            self.add_comp(c)

        # --- the RF domain: one interface per direction, each owning its own metronome ---------
        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.adc_if.bind("tx", self.source.rf_ep)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)

        # --- the PL domain -------------------------------------------------------------------
        # NO depth override, and that is a correction rather than an omission.  These interfaces
        # become the DUT's top-level AXIS ports, and a top-level argument CANNOT carry a FIFO depth:
        # Vitis ignores the pragma (HLS 214-387) and the RTL gets the default of 2.  Declaring 128
        # here made pysim model a 128-word queue the hardware does not have, which is one of the
        # three reasons pysim could not see the ADC dropping words.  `composite_top_spec` now refuses
        # the declaration outright, so this cannot come back by accident.
        adc_axis = StreamIF(name=f"{self.name}_adc_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w)
        adc_axis.bind("master", self.rfdc.rx_stream)
        adc_axis.bind("slave", self.dut.s_in)
        self.add_if(adc_axis)

        dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w)
        dac_axis.bind("master", self.dut.s_out)
        dac_axis.bind("slave", self.rfdc.tx_stream)
        self.add_if(dac_axis)


class RfLoopbackSim:
    """The **procedure** around an :class:`RfLoopbackTB` graph: materialize a scenario, run, check.

    :meth:`write_scenario` is the single scenario writer, so pysim (and, from stage 2, XSI) can never
    start from different bytes — the same discipline ``mem_copy`` follows.
    """

    def __init__(self, n_src_blk: int = 8, name: str = "rf_tb", seed: int = 0xADC0,
                 waveform: str = "grid", sine_freq: float = 1.0e6,
                 **tb_kwargs) -> None:
        tb_kwargs.setdefault("n_blk", n_src_blk)
        self.tb = RfLoopbackTB(name=name, sim=Simulation(), **tb_kwargs)
        #: Blocks the source will play.  Fewer than ``tb.n_blk`` starves the RF grid on purpose.
        self.n_src_blk = int(n_src_blk)
        #: ``"grid"`` (on the quantization grid -- strict packing check, quantizer untested) or
        #: ``"sine"`` (a windowed sinusoid -- exercises the quantizer, and legible in a plot).
        #: See :meth:`_grid_blocks` and :meth:`_sine_blocks` for why both exist.
        if waveform not in ("grid", "sine"):
            raise ValueError(f"waveform must be 'grid' or 'sine', got {waveform!r}")
        self.waveform = waveform
        #: Tone frequency for the ``"sine"`` waveform, in Hz.
        self.sine_freq = float(sine_freq)
        self.seed = int(seed)
        #: The blocks written to the source bundle, kept for the golden.
        self.sent: list[np.ndarray] = []
        self.root: Path | None = None

    # -- the scenario ----------------------------------------------------------------------------

    def write_scenario(self, root) -> None:
        """Write ``<root>/vectors/rf_in`` and point both RF participants at *root*.

        Two waveforms, and the difference between them is not cosmetic — see :meth:`_grid_blocks`
        and :meth:`_sine_blocks`.  Both give an **exact** golden; neither needs a tolerance.
        """
        tb = self.tb
        root = Path(root)
        self.root = root
        self.sent = (self._sine_blocks() if self.waveform == "sine" else self._grid_blocks())
        write_rf_bundle(self.sent, root / "vectors" / "rf_in")
        tb.source.root = root
        tb.sink.root = root

    def _grid_blocks(self) -> list:
        """Random samples drawn **exactly on the converter's quantization grid** —
        ``m / 2^(nbits-1) * full_scale`` for integer ``m``.

        A clean loopback is then *bit*-identical to the input rather than "close", which is what
        makes the packing check strict: a tolerance would hide a packing bug, and packing is what
        this waveform exists to test.  (Exact for any power-of-two ``full_scale``; the divide and
        multiply are both exact in binary floating point.)

        **What it deliberately does NOT test:** quantization.  On-grid samples make ``from_real`` a
        no-op, so rounding and saturation are never exercised.  That is what the sine is for.
        """
        tb = self.tb
        nb = int(tb.nbits)
        fs = float(tb.full_scale)
        rng = np.random.default_rng(self.seed)
        lo, hi = -(1 << (nb - 1)), (1 << (nb - 1)) - 1
        return [rng.integers(lo, hi + 1, size=(1, int(tb.blksize))).astype(np.float64)
                / float(1 << (nb - 1)) * fs
                for _ in range(self.n_src_blk)]

    def _sine_blocks(self) -> list:
        """A windowed sinusoid: ``w(t | t0, t1) * sin(2*pi*f*(t - t0))``, zero outside the window.

        Two things this buys that :meth:`_grid_blocks` does not.

        **It exercises the quantizer.**  A sine does not land on the quantization grid, so
        ``from_real`` really rounds and (near full scale) really saturates — the paths on-grid
        samples skip entirely.  The golden is still exact, just stated against the *quantized* input:
        ``captured[k + L] == to_real(from_real(sent[k]))``.  No tolerance.

        **It is legible.**  A burst with a hard start and stop makes the block-latency shift and the
        leading zero-fill something you can SEE in a plot rather than infer from array indices --
        which is what ``rf_loopback_figures.py`` renders for the guide.
        """
        tb = self.tb
        n = self.n_src_blk * int(tb.blksize)
        t = np.arange(n, dtype=np.float64) / float(tb.samp_rate)
        # The window sits inside the run so both edges are visible, and away from block 0 so the
        # startup zero-fill is not confused with the window being closed.
        t0 = t[n // 4]
        t1 = t[3 * n // 4]
        amp = 0.9 * float(tb.full_scale)          # near full scale, so rounding is exercised
        x = np.where((t >= t0) & (t < t1),
                     amp * np.sin(2.0 * np.pi * self.sine_freq * (t - t0)), 0.0)
        return [x[i * int(tb.blksize):(i + 1) * int(tb.blksize)].reshape(1, -1)
                for i in range(self.n_src_blk)]

    # -- run and check ---------------------------------------------------------------------------

    def run(self, root=None) -> "RfLoopbackTB":
        """Materialize the scenario (into *root*, or a temp dir) and run the SimPy model."""
        if root is not None:
            self.write_scenario(root)
            self.tb.sim.run_sim()
            return self.tb
        with tempfile.TemporaryDirectory() as _root:
            self.write_scenario(_root)
            self.tb.sim.run_sim()
            self._captured = read_rf_bundle(Path(_root) / "vectors" / "rf_out", 1, self.tb.blksize)
            self._in_bytes = (Path(_root) / "vectors" / "rf_in" / "words.bin").read_bytes()
            self._out_bytes = (Path(_root) / "vectors" / "rf_out" / "words.bin").read_bytes()
        return self.tb

    @property
    def captured(self) -> list[np.ndarray]:
        """The sink's capture, read back **from its bundle** rather than off the object."""
        return self._captured

    def check(self) -> "RfLoopbackTB":
        """The stage-1 gate: a loopback that is byte-identical **once shifted by the pipeline's
        declared block latency**, with both loss counters exactly as declared.

        Three claims, and all three are needed.  The data check says the samples survived
        quantization, packing, transport and unpacking unchanged.  The shift check says the loop cost
        exactly what the pipeline declared — no more, no less.  The counter check says nothing was
        *quietly* lost, because backpressure protects against over-production and **nothing** protects
        against under-production, so a run that dropped or zero-filled blocks could still show a
        plausible-looking prefix of correct data.

        The shift is not an artifact to be tolerated.  DAC block *k* carries ADC block *k - L*
        because a loop through the RF grids costs at least one block index, structurally — so the
        first ``L`` DAC periods have nothing to play and emit the zero-fill.  That is the startup
        transient a real converter has, and it is why a design primes its buffer before enabling the
        tile.
        """
        tb = self.tb
        n_lat = int(tb.loop_blk_latency)
        # Fed straight from the source, the ADC edge is entitled to no transient at all.
        tb.adc_if.assert_clean()
        # The DAC edge sits at the end of a pipeline declaring n_lat blocks, so exactly that many of
        # its leading periods have no data.  Checked exactly, and against the grid index too.
        tb.dac_if.assert_clean(startup_blocks=n_lat)

        assert len(self.captured) == self.n_src_blk, (
            f"sink captured {len(self.captured)} blocks, expected {self.n_src_blk}")
        for k in range(n_lat):
            assert not np.any(self.captured[k]), (
                f"block {k} is inside the {n_lat}-block startup transient, so it must be the "
                f"zero-fill the DAC emits when its samples have not arrived yet")
        for k in range(n_lat, self.n_src_blk):
            assert np.array_equal(self.sent[k - n_lat], self.captured[k]), (
                f"DAC block {k} != ADC block {k - n_lat} after the loopback")

        # The same claim at the byte level, on the bundles both backends will share from stage 2.
        blk_bytes = int(tb.blksize) * 8          # n_ch=1, float64
        assert self._out_bytes[n_lat * blk_bytes:] == self._in_bytes[:len(self._out_bytes)
                                                                     - n_lat * blk_bytes], (
            f"the sink's bundle is not byte-identical to the source's once shifted by the declared "
            f"{n_lat}-block latency")
        assert tb.dut.n_blk == self.n_src_blk, (
            f"DUT relayed {tb.dut.n_blk} bursts, expected {self.n_src_blk}")
        self.check_alignment()
        return tb

    def check_alignment(self) -> None:
        """Assert TX/RX alignment as a **derived** quantity, not a scheduling coincidence.

        ``t0`` plus a rate defines the grid, so the time of any sample on either side follows from
        two numbers and alignment is arithmetic.  With both tiles on one epoch — the normal case, and
        what MTS is for — DAC sample *n* occurs at exactly the same instant as ADC sample *n*.

        Nothing about the run can make this true by luck: if the epochs were emergent from whoever
        happened to be scheduled first, it would not hold at every *n*.  And note what is **not**
        being claimed — that the two grids being aligned means the loop is free.  The loop still costs
        ``loop_blk_latency`` block *indices*; alignment is about when the grids tick, latency is about
        which block each tick carries.  Keeping those separate is why neither has to fudge the other.
        """
        tb = self.tb
        for n in (0, int(tb.blksize), int(tb.blksize) * max(self.n_src_blk - 1, 0)):
            got = tb.dac_if.samp_time(n) - tb.adc_if.samp_time(n)
            assert abs(got) < 1e-15, (
                f"sample {n}: the DAC grid is {got:g}s off the ADC grid; both tiles share one epoch, "
                f"so they should be aligned")


def run_loopback(n_src_blk: int = 8, **tb_kwargs) -> "RfLoopbackTB":
    """Run the clean loopback and check it — the stage-1 gate in one call."""
    sim = RfLoopbackSim(n_src_blk=n_src_blk, **tb_kwargs)
    sim.run()
    return sim.check()


def run_and_check() -> bool:
    tb = run_loopback()
    print(f"[rf_loopback] adc {tb.adc_if.counters()}")
    print(f"[rf_loopback] dac {tb.dac_if.counters()}")
    print("rf_loopback pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
