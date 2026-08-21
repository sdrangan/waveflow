"""``RfCircPlay`` — the repeat scheduler moved **into the DUT**, in pysim.

Stage 1's testbench problem was that this behaviour is *reactive*: it issues ``start_now``, waits for
the ``TxResp`` to learn where the waveform landed, and schedules every later play at
``samp_start + k*PERIOD``.  A stimulus that depends on the DUT's own output cannot be written into a
vector file before the run.  So it moved into fabric, and the testbench's whole job became **push
``NSAMP`` words once**.

**What that costs, stated first.**  The fault injections that drive Stage 1's assertions 2 and 3 — a
window aimed into the past, a starved host — were *testbench* behaviours. A testbench that only
pushes a waveform cannot express them, so those two assertions keep their coverage from
``tests/examples/test_rf_repeat_play.py``, which drives the **same** loader and player from pysim.
That is a different stimulus for one design, not a second model of one behaviour.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_repeat_play.rf_repeat_play import (
    BLK_SAMP,
    LEAD,
    NSAMP,
    N_BLK,
    PERIOD,
    played_samples,
    run_circ_pysim,
    waveform,
)

#: Converter block the ``start_now`` window lands in, counting the grid from 0.  Measured, then
#: pinned: block 0 is gone before the first command has crossed the loader.
FIRST_PLAY_BLK = 1


def _blocks(played: np.ndarray) -> list[np.ndarray]:
    return [played[j * BLK_SAMP:(j + 1) * BLK_SAMP] for j in range(played.size // BLK_SAMP)]


def _empty(played: np.ndarray) -> set[int]:
    return {j for j, b in enumerate(_blocks(played)) if not b.any()}


class TestTheSchedulerRunsInFabric:

    @pytest.fixture(scope="class")
    def tb(self):
        return run_circ_pysim()

    def test_the_testbench_pushes_the_waveform_once_and_nothing_else(self, tb):
        """The whole point of moving the host in.

        One burst, from a file-driven :class:`~waveflow.simulation.stream_tb.StreamDriver` — the same
        BFM the XSI harness uses, so the two backends provably start from identical bytes. Every
        command after that is generated inside the design.
        """
        assert tb.dut.sched.n_reloads == 1, "exactly one waveform was ever pushed"
        assert tb.dut.sched.tid > 10, (
            "and the design issued its own commands afterwards — a tid that never advanced would "
            "mean the scheduler stalled and the playout came from somewhere else")

    def test_every_play_lands_on_the_absolute_grid(self, tb):
        """``base + k*PERIOD``, learned once and never recomputed.

        ``base`` comes from the ``start_now`` response and from nothing else; ``k`` counts up. A
        scheduler that drifted — or that re-based on anything — would show as a block carrying the
        waveform at the wrong phase, which is asserted directly at the converter below.
        """
        s = tb.dut.sched
        assert s.base == FIRST_PLAY_BLK * BLK_SAMP
        assert s.n_late == 0, (
            f"{s.n_late} window(s) came back TX_TOO_LATE. The scheduler aimed at a slot that had "
            f"already gone — see the k = LEAD note in RfCircPlay: starting the train at k = 1 makes "
            f"every SECOND play late, forever, which reads like a throughput problem.")
        assert s.n_no_slot == 0, "the scheduler honours max_in_flight by construction"
        assert s.outstanding == LEAD, "and it is still LEAD deep when the run ends"

    def test_the_playout_is_the_waveform_in_every_block_but_the_startup_hole(self, tb):
        played = played_samples(tb)
        assert played.size == N_BLK * BLK_SAMP
        want = waveform()
        # Block 0 is gone before the first command crosses.  Blocks 2..LEAD are the price of a
        # DEFERRED response: the verdict for the start_now window arrives when its last sample has
        # played, so the first schedulable slot is LEAD periods out.  Bounded and derived, not a
        # tuning residue.
        assert _empty(played) == {0} | set(range(FIRST_PLAY_BLK + 1, FIRST_PLAY_BLK + LEAD))
        for j, b in enumerate(_blocks(played)):
            if j in _empty(played):
                continue
            assert np.array_equal(b, want), (
                f"block {j} carries something other than the waveform — a play at the wrong phase")

    def test_the_converter_was_fed_and_the_counters_agree(self, tb):
        assert tb.dac_if.overrun == 0
        assert tb.dac_if.underrun == LEAD, "block 0 plus the LEAD-1 startup hole"
        c = tb.dut.tx.counters
        assert c["n_too_late"] == c["n_missed"] == 0
        assert c["n_status_dropped"] == 0
        assert c["n_transmitted"] == tb.dut.sched.n_played
        tb.dut.tx.assert_clean()

    def test_period_equals_nsamp_is_the_case_that_needs_lead(self, tb):
        """Back-to-back replay is the configuration a blocking body cannot serve.

        The verdict for play *k* arrives at slot ``base + k*PERIOD + NSAMP``; command *k+1* is due at
        ``base + (k+1)*PERIOD``. At ``PERIOD == NSAMP`` that lead is **zero**, so a scheduler that
        blocked on each response would underrun by construction. This scenario is exactly that case,
        and it does not underrun past the startup hole — which is what says ``LEAD`` is doing its job
        rather than merely being set.
        """
        assert PERIOD == NSAMP, "the scenario under test is back-to-back replay"
        assert LEAD >= 2
        assert tb.dac_if.last_underrun_idx <= FIRST_PLAY_BLK + LEAD, (
            "an underrun after the startup hole means the scheduler fell behind in steady state")
