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

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.rf_sample_if import RFSampIF
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

    #: AXIS word width in bits — ``samp_per_word * nbits`` at the converter.  Read off the
    #: :class:`~examples.rf_loopback.rfdc.Rfdc` when the testbench builds the graph.
    bitwidth: HwParam[int] = 64
    #: Words in one block's burst — ``blksize / samp_per_word``.
    nwords_blk: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.s_out)
        #: Bursts relayed — the sanity number the check reads.
        self.n_blk = 0

    def run_iter(self) -> ProcessGen[None]:
        words = yield from self.s_in.get(nwords_max=int(self.nwords_blk))
        self.n_blk += 1
        yield from self.s_out.write(words)


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
    #: The **ADC** tile's epoch, pushed to the ADC interface at bind.
    t0: float = 0.0
    #: How much later the **DAC** tile's epoch is than the ADC's, in **block periods**.
    #:
    #: This is the one number a loopback cannot leave at zero, and the reason is worth stating: the
    #: DAC grid is a metronome, not a queue.  It emits a block every period whether or not the
    #: samples for it have finished their trip through the fabric, and a period that arrives first
    #: is an underrun — zero-filled and counted.  So the DAC tile has to be *started later* than the
    #: ADC tile by at least the fabric round trip, and ``t0`` is where a design says so.  One block
    #: period is comfortably more than the two AXIS bursts this pipeline costs.
    dac_lag_blk: float = 1.0
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
                         t0_rx=float(self.t0),
                         t0_tx=float(self.t0) + float(self.dac_lag_blk) * self.blk_period)
        w = self.rfdc.axis_bitwidth
        self.nwords_blk = int(self.blksize) // int(self.samp_per_word)

        self.dut = RfSampPassThrough(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                                     nwords_blk=self.nwords_blk)
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
        """The stage-1 gate: a byte-identical loopback with both loss counters at zero.

        Two separate claims, and both are needed.  The data check says the samples survived
        quantization, packing, transport and unpacking unchanged.  The counter check says nothing was
        *quietly* lost on the way — backpressure protects against over-production and **nothing**
        protects against under-production, so a run that dropped or zero-filled blocks could still
        show a plausible-looking prefix of correct data.
        """
        tb = self.tb
        tb.adc_if.assert_clean()
        tb.dac_if.assert_clean()
        assert len(self.captured) == self.n_src_blk, (
            f"sink captured {len(self.captured)} blocks, expected {self.n_src_blk}")
        for k, (exp, got) in enumerate(zip(self.sent, self.captured)):
            assert np.array_equal(exp, got), f"block {k} differs after loopback"
        assert self._in_bytes == self._out_bytes, (
            "the sink's bundle is not byte-identical to the source's")
        assert tb.dut.n_blk == self.n_src_blk, (
            f"DUT relayed {tb.dut.n_blk} bursts, expected {self.n_src_blk}")
        self.check_alignment()
        return tb

    def check_alignment(self) -> None:
        """Assert TX/RX alignment as a **derived** quantity, not a scheduling coincidence.

        ``t0`` plus a rate defines the grid, so the time of any sample on either side follows from
        two numbers and alignment is arithmetic.  The relation held here is the one the design
        configured: DAC sample *n* occurs exactly ``dac_lag_blk`` block periods after ADC sample *n*.
        Nothing about the run can make this true by luck — if the epochs were emergent from whoever
        happened to be scheduled first, it would not hold at every *n*.
        """
        tb = self.tb
        want = float(tb.dac_lag_blk) * tb.blk_period
        for n in (0, int(tb.blksize), int(tb.blksize) * max(self.n_src_blk - 1, 0)):
            got = tb.dac_if.samp_time(0, n) - tb.adc_if.samp_time(0, n)
            assert abs(got - want) < 1e-15, (
                f"sample {n}: DAC grid is {got:g}s after the ADC grid, expected {want:g}s")


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
