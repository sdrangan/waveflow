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
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.synth import sim_only
from waveflow.simulation.rf_tb import RfDataSink, RfDataSource, read_rf_bundle, write_rf_bundle
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

from examples.rf_loopback.rfdc import Rfdc


@dataclass
class RfSampPassThrough(FreeRunMod):
    """The digital logic: one AXIS burst in, the same burst out.

    A free-running module, because that is what the fabric between two converter ports is — it has no
    host to start it and re-fires on each arriving block.  Its body is trivial on purpose: stage 1 is
    about the converter boundary, and a pass-through makes the loopback golden exact rather than
    approximately equal.  Stage 3's ``RfSampBuf`` is the first block that does something.
    """

    #: The generated top's name.  Without it the module has no codegen identity and
    #: ``tb_top_spec`` has no ``top_name`` — which, not the boundary, is what actually kept this
    #: DUT out of a testbench walk.
    cpp_kernel_name: ClassVar[str | None] = "rf_pass_through"

    #: AXIS word width in bits — ``samp_per_word * nbits`` at the converter.  Read off the
    #: :class:`~examples.rf_loopback.rfdc.Rfdc` when the testbench builds the graph.
    bitwidth: HwParam[int] = 64
    #: Words in one block's burst — ``blksize / samp_per_word``.
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
    #: This is a **declaration that gets checked**, not a knob: the testbench asserts the downstream
    #: edge underran exactly this many times at startup, so a module that claims two blocks and
    #: exhibits one fails.  (Wall-clock latency here is sub-block — the fabric is ~4.7x oversized for
    #: this rate — but a design must not lean on that: sub-block timing is precisely what block-LT
    #: does not resolve.)
    blk_latency: HwParam[int] = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        if int(self.blk_latency) < 1:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': blk_latency must be >= 1 block, got "
                f"{int(self.blk_latency)}. A zero-latency loop through the RF grids is not a system "
                f"that can exist — block k cannot be played in the period it was captured — so it is "
                f"refused here rather than reported later as an underrun.")
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.s_out)
        #: Bursts relayed — the sanity number the check reads.  Incremented through
        #: :meth:`count_burst`, never inline: see there for why.
        self.n_blk = 0

    @sim_only
    def count_burst(self) -> None:
        """Tally one relayed burst — **instrumentation, and marked as such.**

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
        """The payload type of one firing: ``DataArray`` of ``nwords_blk`` words of ``bitwidth``.

        The **instance → type bridge**: the module's ``HwParam``s feed a schema specialization, so
        one declaration serves every configuration the pysim tests sweep *and* pins a concrete type
        at extract time (which elaborates at the defaults).

        Reading ``self.blk_words`` inside :meth:`run_iter` is allowed by the implicit-capture rule
        precisely because it resolves to a ``DataSchema`` subclass — a type is a fact about the
        design, not storage someone has to write.
        """
        return DataArray.specialize(
            element_type=IntField.specialize(bitwidth=int(self.bitwidth), signed=False),
            max_shape=(int(self.nwords_blk),), static=True)

    def run_iter(self) -> ProcessGen[None]:
        # The TYPED get, not the raw-word ``get(nwords_max=...)`` form.  That form is documented as
        # the "old (raw-word) calling convention ... used by non-HwModule callers such as PolyTB",
        # and the extractor has no rule for it — it reaches for the schema type that a raw get does
        # not carry.  A synthesizable body uses the typed convention.
        blk = yield from self.s_in.get(self.blk_words)
        self.count_burst()
        yield from self.s_out.write(blk)


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
                                     nwords_blk=self.nwords_blk)
        #: Block latency of the wired path from the ADC edge to the DAC edge — **summed from what the
        #: modules on that path declare**, not inferred.  One module here, so it is the DUT's.  A
        #: general graph would need loop detection; a design that states its latency does not.  This
        #: is the DAC edge's entitled startup transient.
        self.loop_blk_latency = int(self.dut.blk_latency)
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
        # Depth is a physical property of the channel and is read by pysim as the queue bound: two
        # blocks, so the pass-through is not the thing throttling the RF grid.
        adc_axis = StreamIF(name=f"{self.name}_adc_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w, depth=self.nwords_blk * 2)
        adc_axis.bind("master", self.rfdc.rx_stream)
        adc_axis.bind("slave", self.dut.s_in)
        self.add_if(adc_axis)

        dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w, depth=self.nwords_blk * 2)
        dac_axis.bind("master", self.dut.s_out)
        dac_axis.bind("slave", self.rfdc.tx_stream)
        self.add_if(dac_axis)


class RfLoopbackSim:
    """The **procedure** around an :class:`RfLoopbackTB` graph: materialize a scenario, run, check.

    :meth:`write_scenario` is the single scenario writer, so pysim (and, from stage 2, XSI) can never
    start from different bytes — the same discipline ``mem_copy`` follows.
    """

    def __init__(self, n_src_blk: int = 8, name: str = "rf_tb", seed: int = 0xADC0,
                 **tb_kwargs) -> None:
        tb_kwargs.setdefault("n_blk", n_src_blk)
        self.tb = RfLoopbackTB(name=name, sim=Simulation(), **tb_kwargs)
        #: Blocks the source will play.  Fewer than ``tb.n_blk`` starves the RF grid on purpose.
        self.n_src_blk = int(n_src_blk)
        self.seed = int(seed)
        #: The blocks written to the source bundle, kept for the golden.
        self.sent: list[np.ndarray] = []
        self.root: Path | None = None

    # -- the scenario ----------------------------------------------------------------------------

    def write_scenario(self, root) -> None:
        """Write ``<root>/vectors/rf_in`` and point both RF participants at *root*.

        The samples are drawn **exactly on the converter's quantization grid** —
        ``m / 2^(nbits-1) * full_scale`` for integer ``m`` — so a clean loopback is *bit*-identical
        rather than "close".  That is deliberate: a tolerance would hide a packing bug, and packing
        is the thing this example is testing.  (It is exact for any power-of-two ``full_scale``; the
        divide and multiply are then both exact in binary floating point.)
        """
        tb = self.tb
        root = Path(root)
        self.root = root
        nb = int(tb.nbits)
        fs = float(tb.full_scale)
        rng = np.random.default_rng(self.seed)
        lo, hi = -(1 << (nb - 1)), (1 << (nb - 1)) - 1
        self.sent = []
        for _ in range(self.n_src_blk):
            m = rng.integers(lo, hi + 1, size=(1, int(tb.blksize)))
            self.sent.append(m.astype(np.float64) / float(1 << (nb - 1)) * fs)
        write_rf_bundle(self.sent, root / "vectors" / "rf_in")
        tb.source.root = root
        tb.sink.root = root

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
