"""rf_relayout.py — the Stage A gate on the **logic-side re-layout**, at 14-in-16.

``plans/rf_shot_buf.md`` § *The caveat, and it is a Stage A gate*.

The shot buffer's logic-side port carries **densely-packed effective-width samples**, not
:class:`~waveflow.hw.rfdc_samp_word.RfdcSampWord`, so the buffer owns the converter's packing and
nothing upstream of it has to know about ``justify`` or 14-in-16.  ``plans/adc_model.md`` predicted
that "shift and mask per slot holds II=1" — and flagged that **the prediction was untestable in this
repo**, because every configuration except the RFSoC 4x2 preset has
``bits_per_samp == bits_per_samp_pack``, which makes the whole conversion the identity.

This example is the configuration that is not.  ``Rfsoc4x2SampWord`` at four samples per beat: 14
effective bits in a 16-bit slot, left-justified, four to a 64-bit word.  :data:`WORD` therefore has a
nonzero :meth:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.justify_shift`, and
:attr:`~waveflow.hw.rf_relayout.RfRelayout.is_identity` is ``False`` — which a test asserts, because
a gate that silently degraded to the identity would keep passing while measuring nothing.

::

    StreamDriver --> RfRelayout.to_dense --dense--> RfRelayout.to_slots --> StreamSink

**A loopback, and graded twice.**  At RTL the output must equal the *stimulus*, which needs no second
implementation of the conversion to compare against; and in pysim the **intermediate** dense words
are compared byte-for-byte against :func:`~waveflow.hw.rf_relayout.to_dense`, which is the half a
loopback cannot see (a pair of wrong-but-inverse conversions round-trips perfectly).

**Why the ramp steps by 4.**  A left-justified 14-in-16 slot has two low bits the converter never
sets, so ``to_slots(to_dense(x)) == x`` holds only for slot values that respect that.  Building the
stimulus from *codes* through :func:`~waveflow.hw.rfdc_samp_word.pack` makes that automatic — the
same reason ``examples/rf_samp_buf_rx`` has a ``SAMP_STEP``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.interface import StreamIF  # noqa: E402
from waveflow.hw.rf_relayout import RfRelayout, to_dense  # noqa: E402
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord, pack  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

__all__ = ["WORD", "NWORD", "XSI_N_CYCLES", "RfRelayout", "RfRelayoutTB", "check_outputs",
           "check_xsi_outputs", "dense_golden", "run_pysim", "stim_words", "write_scenario"]

#: **The whole point of this example**: a word whose effective and container widths differ.  Change
#: it to anything with ``bits_per_samp == bits_per_samp_pack`` and the design under test becomes a
#: pair of wires.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)

#: Words driven through.  Small: the II is read off the csynth report, not inferred from a cycle
#: count, so a long run buys nothing here.
NWORD = 64

#: The ramp's first converter code, and a value near **full scale** deliberately included below: the
#: "shift inside the narrow type" mistake in ``rf_relayout_to_slots_task.h`` only shows up on a
#: sample whose top bits are set, and a ramp starting at 1000 would never produce one.
CODE_BASE = 1000

XSI_N_CYCLES = 2000


def stim_codes(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The stimulus, in converter codes: a ramp, with the four extremes pinned into the first word.

    ``[min, max, -1, 0]`` first, then the ramp.  The extremes are what catch a shift performed in the
    wrong width — a full-scale sample that comes back small is a *signal-level* error, not a crash,
    and a ramp that never approaches full scale would pass it.
    """
    eff = int(WORD.bits_per_samp)
    lo, hi = -(1 << (eff - 1)), (1 << (eff - 1)) - 1
    n = int(nword) * int(WORD.samp_per_word)
    ramp = ((np.arange(n - 4, dtype=np.int64) + int(base) - lo) % (1 << eff)) + lo
    return np.concatenate([np.array([lo, hi, -1, 0], dtype=np.int64), ramp])


def stim_words(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The stimulus as packed **converter** words — through :func:`~waveflow.hw.rfdc_samp_word.pack`.

    Packing from codes rather than writing slot values by hand is what makes the low two bits zero
    without anyone having to remember that they must be.
    """
    return np.asarray(pack(WORD, stim_codes(nword, base).reshape(1, -1)), dtype=np.uint64).ravel()


def dense_golden(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The **intermediate** words — what the dense port must carry, byte for byte.

    The half a loopback cannot check.  Two inverse-but-wrong conversions round-trip perfectly, so an
    identity at the boundary says nothing about the format in the middle; this is what says it.
    """
    return to_dense(WORD, stim_words(nword, base))


def write_scenario(root, nword: int = NWORD) -> None:
    """Materialize ``<root>/vectors/stim`` — one word per burst, for ``bram_toy``'s reason."""
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle([np.array([x], dtype=np.uint64) for x in stim_words(nword)],
                       Path(root) / "vectors" / "stim")


def check_outputs(got, nword: int = NWORD, where: str = "") -> None:
    """The loopback claim: converter word in, the **same** converter word out.

    The two diagnoses worth naming, because both are shift errors and they look nothing alike in a
    hex dump:

    * every word **scaled** — the shift ran in one direction only, or by the wrong amount;
    * only the **large** samples wrong — the widening happened after the shift instead of before, so
      the top bits fell off the narrow type.  This is why the stimulus pins full scale into word 0.
    """
    want = stim_words(nword)
    got = np.asarray(got, dtype=np.uint64).ravel()
    if got.size != want.size:
        raise AssertionError(
            f"{where}rf_relayout returned {got.size} words for {want.size} driven in.")
    if np.array_equal(got, want):
        return
    bad = int(np.argmax(got != want))
    extra = ""
    if bad == 0:
        extra = (" — word 0 carries the four EXTREME codes (min, max, -1, 0), so a failure here is "
                 "the widen-after-shift bug: the sample was shifted inside the narrow dense type "
                 "and its top bits fell off.")
    raise AssertionError(
        f"{where}rf_relayout word {bad}: 0x{int(got[bad]):016x} != 0x{int(want[bad]):016x} "
        f"({int((got != want).sum())} of {want.size} words differ){extra}")


@dataclass
class RfRelayoutTB(FreeRunMod):
    """A driver, the two conversions back to back, and a sink."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    nword: int = NWORD
    n_cycles: int = XSI_N_CYCLES
    axis_freq: float = RFSOC4X2_CLK_HZ
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        w = int(WORD.bitwidth)
        self.dut = RfRelayout.for_word(WORD, sim=self.sim, name=f"{self.name}_dut",
                                       clk=self.axis_clk)
        self.drv = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                in_bundle="vectors/stim", has_tlast=True)
        self.sink = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=w,
                               out_bundle="vectors/out", has_tlast=True)
        for c in (self.dut, self.drv, self.sink):
            self.add_comp(c)

        in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.axis_clk, bitwidth=w)
        in_if.bind(ep_name="master", endpoint=self.drv.stream_ep)
        in_if.bind(ep_name="slave", endpoint=self.dut.s_in)
        self.add_if(in_if)

        out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.axis_clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=self.dut.s_out)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)


def check_xsi_outputs(xsi_dir, nword: int = NWORD, want_cycles: int | None = None) -> None:
    """Check an XSI run from the bundle it dumped."""
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    assert (vdir / "out").is_dir(), f"no capture bundle at {vdir / 'out'} — the run did not dump one"
    got = np.concatenate(read_burst_bundle(vdir / "out"))
    check_outputs(got, nword, where="XSI: ")
    if want_cycles is not None:
        cycles = np.fromfile(vdir / "out" / "cycles.bin", dtype="<u8")
        last = int(cycles[-1])
        assert last == want_cycles, (
            f"rf_relayout completed at cycle {last}, gate expects {want_cycles}. That is a real "
            f"behaviour change: either a regression or an improvement worth re-recording.")


def run_pysim(root=None, nword: int = NWORD) -> RfRelayoutTB:
    """Run the graph in SimPy and return the testbench — the toolchain-free golden."""
    import tempfile

    tb = RfRelayoutTB(name="tb", sim=Simulation(), nword=int(nword))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(root or tmp)
        write_scenario(root, nword)
        tb.drv.root = root
        tb.sink.root = root
        tb.sim.run_sim()
    return tb


def captured_words(tb: RfRelayoutTB) -> np.ndarray:
    return np.concatenate(tb.sink.words) if tb.sink.words else np.zeros(0, dtype=np.uint64)
