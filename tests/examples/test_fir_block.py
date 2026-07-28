"""The block FIR: two flavours of ``add_state`` in one module, checked against a stateless golden.

``examples/state_toy`` gates that *a* static survives re-firing.  This gates the design that motivated
``add_state`` in the first place (``plans/add_state.md``): a module holding **held** coefficients and a
**per-block carry** at the same time, dispatched from one compute leaf on an opcode.

The golden is deliberately not the DUT's algorithm: it convolves the whole signal with globally-indexed
history, so "block-wise output == global convolution" *is* the statement that the carry is right.  The
falsification tests below then prove the gate can fail — separately for each flavour of state, because
a check that only notices one of them would let the other rot.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.fir_block.fir_block import FirCompute, FirOp, samp_type
from examples.fir_block.fir_block_sim import (
    DEFAULT_PROGRAM,
    FirBlockSim,
    _stimulus,
    _tap_set,
)


def test_gate_program_reloads_mid_stream():
    """Guard the scenario itself: the program must have >=3 filter firings (so the carry is exercised
    beyond the first block's zeros) and a reload *after* the first filter (so the held state is proved
    replaceable).  Weaken the program and this gate goes quiet without any test turning red."""
    steps = list(DEFAULT_PROGRAM)
    assert steps.count("filter") >= 3, f"program {steps} exercises the carry too little"
    first_filter = steps.index("filter")
    assert any(s == "load" for s in steps[first_filter:]), (
        f"program {steps} never reloads mid-stream — a stale tap set would pass")


def test_tap_sets_and_stimulus_discriminate():
    """Guard the discriminator: a stale tap set has to give a *loudly* wrong answer, so the two sets
    must differ everywhere and the stimulus must not be mostly zeros."""
    samp = samp_type(16, 2)
    h0, h1 = _tap_set(0, 32, samp), _tap_set(1, 32, samp)
    assert np.all(h0 != h1), "the two tap sets overlap — a missed reload could pass"
    x = _stimulus(64, 0, samp)
    assert np.count_nonzero(x) > len(x) // 2, "stimulus is mostly zeros; a wrong carry would hide"


def test_pysim_matches_global_convolution():
    """THE gate: three blocks filtered with per-block carry equal the whole signal convolved once,
    across a mid-stream coefficient reload.  Also asserts one completion per command **in issue
    order** — including the no-output ``LOAD_TAPS``, whose token path is the plan's deadlock risk."""
    FirBlockSim().run()


def test_pysim_single_block():
    """The degenerate program: one load, one block.  With ``zero_state`` the first block's history is
    zeros, so this is the plain convolution and needs no carry at all."""
    FirBlockSim(program=("load", "filter"), blk=32).run()


@pytest.mark.parametrize("samp_w,samp_i", [(8, 2), (12, 2), (18, 3)])
def test_pysim_across_sample_widths(samp_w, samp_i):
    """The format algebra carries the design, not a hand-derived accumulator width: the same source
    holds at 8, 12, and 18 bits.  (18 is the interesting one — a DSP48E1 is a 25x18 multiplier, so it
    is the last width whose per-tap multiply still fits one DSP.)"""
    FirBlockSim(program=("load", "filter", "filter"), blk=32, ntap=8,
                samp_w=samp_w, samp_i=samp_i).run()


def test_both_realizations_share_one_golden():
    """``unroll_lane`` is a QoR knob, not a design change: both bodies must satisfy the *same* golden.

    That is the property that makes the pair a monomorphized-QoR probe — if the two ever disagreed
    numerically, comparing their DSP/throughput would be comparing two different filters."""
    for unroll in (False, True):
        FirBlockSim(program=("load", "filter", "filter"), blk=32, unroll_lane=unroll).run()


def test_realization_selects_the_task_body_and_its_throughput():
    """The parameter must actually reach codegen *and* the timing model — a knob that silently does
    nothing would still pass the golden above."""
    serial = FirBlockSim(program=("load", "filter"), blk=64, unroll_lane=False).tb.dut.compute
    unroll = FirBlockSim(program=("load", "filter"), blk=64, unroll_lane=True).tb.dut.compute

    assert serial.kernel_task().task_fn == "fir_compute_serial_task"
    assert unroll.kernel_task().task_fn == "fir_compute_unroll_task"
    assert serial.kernel_task().header != unroll.kernel_task().header

    # LW = 32/16 = 2, so the unrolled body retires a 64-sample block in half the beats.
    assert unroll.lw == 2, f"expected LW=2 at MEM_DW=32, W=16; got {unroll.lw}"
    assert unroll._compute_delay(64) < serial._compute_delay(64), (
        "the unrolled realization must model fewer beats per block, or the sweep measures nothing")


def test_carry_is_the_previous_block_tail():
    """State is *observable*, not just inferred from outputs: after a block, ``carry`` holds that
    block's last ``T-1`` samples, which is what the next firing's window needs."""
    sim = FirBlockSim(program=("load", "filter"), blk=32, ntap=8)
    dut = sim.run()
    compute: FirCompute = dut.compute
    tail = np.asarray(compute.carry.val, dtype=np.int64)
    x = _stimulus(32, 0, compute.samp_cls)
    np.testing.assert_array_equal(tail, x[-(int(compute.ntap) - 1):])


def test_broken_carry_fails_the_gate(monkeypatch):
    """A gate that cannot fail is not a gate — half one.  Ignore the carry (start every block from
    zeros) and the golden must reject it."""
    orig = FirCompute.filter_block
    monkeypatch.setattr(FirCompute, "filter_block",
                        lambda self, x, n, taps, carry, zero_state: orig(self, x, n, taps, carry, 1))
    with pytest.raises(AssertionError, match="fir_block block"):
        FirBlockSim().run()


def test_broken_tap_reload_fails_the_gate(monkeypatch):
    """The other half: honour only the *first* ``LOAD_TAPS`` and the golden must reject the block
    that runs after the mid-stream reload."""
    orig = FirCompute.load_taps

    def load_once(self, x, n, taps):
        if getattr(self, "_loaded", False):
            return
        self._loaded = True
        return orig(self, x, n, taps)

    monkeypatch.setattr(FirCompute, "load_taps", load_once)
    with pytest.raises(AssertionError, match="fir_block block"):
        FirBlockSim().run()


def test_load_taps_writes_nothing_but_still_completes():
    """The no-output opcode, stated as its own property: a ``LOAD_TAPS`` job issues a ``len=0`` write
    (so the writer's store loop trips zero times and no AXI transaction happens) and *still* lands a
    completion.  This is the shape that has deadlocked this codebase twice before."""
    sim = FirBlockSim(program=("load", "filter"), blk=32, ntap=8)
    dut = sim.run()
    loads = [s for s in sim.tb._steps if s["op"] == FirOp.LOAD_TAPS]
    assert len(loads) == 1
    # Both firings are recorded by the writer, but only the FILTER moved words.
    assert len(dut.wstream.fire_log) == len(sim.tb._steps), (
        "the writer must fire once per command, including the zero-length one")
