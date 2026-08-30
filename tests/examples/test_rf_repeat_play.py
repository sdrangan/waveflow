"""``examples/rf_repeat_play`` — Stage 1 of ``plans/rf_samp_new.md``, in pysim.

**An all-green repeat test proves very little**, so three assertions carry this example and all three
are made to fire:

=================================  =========================================================
``TestTheScheduleHolds``           consecutive plays land exactly ``PERIOD`` apart, over
                                   enough repeats that *drift* shows and not only a jump
``TestTooLateIsDrivenOffZero``     one play aimed into the past comes back ``TX_TOO_LATE`` —
                                   and **every other tid is unaffected**, which is what
                                   proves the positional pairing survived a refusal
``TestUnderrunRecoversOnTheGrid``  starve the host, then confirm the schedule resumes at
                                   ``samp_start + k*PERIOD`` rather than re-based on the gap
=================================  =========================================================

The played stream is read off the **RF sink** — the far side of the converter — and block ``j`` of it
covers slots ``[j*BLK_SAMP, (j+1)*BLK_SAMP)`` with underruns present as zeros.  So "which blocks were
played" is a set of integers, and every assertion below is an equality between two sets of integers
rather than a count.  A count would pass a design that played the right number of blocks in the wrong
places, which is exactly what a drifting schedule does.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_repeat_play.rf_repeat_play import (
    BLK_SAMP,
    FIRST_PLAY_BLK,
    N_BLK,
    NREPEAT,
    NSAMP,
    PERIOD,
    START_LEAD,
    TX_MISALIGNED,
    TX_NO_SLOT,
    TX_TOO_LATE,
    TX_TRANSMITTED,
    TX_ZERO_LEN,
    played_samples,
    run_pysim,
    waveform,
)


# ---------------------------------------------------------------------------
# Reading the playout
# ---------------------------------------------------------------------------


def _blocks(played: np.ndarray) -> list[np.ndarray]:
    n = played.size // BLK_SAMP
    return [played[j * BLK_SAMP:(j + 1) * BLK_SAMP] for j in range(n)]


def _filled(played: np.ndarray) -> set[int]:
    """Indices of the blocks that carry real data.  Everything else the edge zero-filled."""
    return {j for j, b in enumerate(_blocks(played)) if b.any()}


def _scheduled_ks(prime_now: int = 1, starve: range | None = None) -> set[int]:
    """Which play indices the host actually issues, given the priming and any starvation.

    ``k`` is both the ``tid`` and the multiplier in ``base + k*PERIOD``, so this set *is* the
    schedule — and with ``PERIOD == BLK_SAMP`` the block a play lands in is ``FIRST_PLAY_BLK + k``.
    """
    ks = set(range(int(prime_now))) | set(range(max(int(prime_now), START_LEAD), NREPEAT))
    return ks - set(starve or [])


def _play_block(k: int) -> int:
    return FIRST_PLAY_BLK + k


# ---------------------------------------------------------------------------
# 1.  The schedule holds
# ---------------------------------------------------------------------------


class TestTheScheduleHolds:
    """Every play lands exactly ``PERIOD`` slots after the one before it, for the whole run."""

    @pytest.fixture(scope="class")
    def tb(self):
        return run_pysim()

    def test_the_start_now_window_is_the_only_one_that_had_to_be_discovered(self, tb):
        """``start_now`` on the first play, and nothing else — the host's whole knowledge of "now".

        ``TxResp.samp_start`` for ``tid`` 0 is ``status.slot - (nsamp - 1)``, recovered by the loader
        from where the window's **last** sample actually went out.  Nothing else in the run is
        discovered; every later slot is arithmetic on this one.
        """
        assert tb.host.base == FIRST_PLAY_BLK * BLK_SAMP, (
            f"the start_now window landed at slot {tb.host.base}, not block {FIRST_PLAY_BLK}. That "
            f"is the loader's latency, pinned as a gate constant — a change is a finding, not a "
            f"number to re-tune.")
        first = tb.host.resps[0]
        assert first[:2] == (0, TX_TRANSMITTED)
        assert first[2] == tb.host.base
        assert sum(1 for r in tb.host.resps if r[0] != 0 and r[1] == TX_TRANSMITTED) == len(
            tb.host.resps) - 1, "every other window was scheduled absolutely and every one played"

    def test_consecutive_plays_land_exactly_one_period_apart(self, tb):
        """The response for every ``tid`` says where its window went out.  Differenced, that is the
        schedule — and it must be a constant ``PERIOD``, not merely periodic on average.

        Thirty-seven plays.  **Enough that drift shows, not just a jump**: the defect this guards
        against is a relative ``timeout(blk_period)`` in place of the absolute grid, which adds the
        body's own elapsed time to every period, so its error accumulates linearly with the firing
        index.  A single fabric cycle of slip per firing (4 ns against a 15.6 ns slot) would put the
        schedule a whole slot out by play 4 and half a period out by play 40 — long before the run
        ends, and visible here as a difference that is not ``PERIOD``.
        """
        starts = [r[2] for r in sorted(tb.host.resps)]
        ks = sorted(_scheduled_ks())
        assert len(starts) == len(ks) == 37
        assert starts == [tb.host.base + k * PERIOD for k in ks], (
            "at least one window went out at a slot the schedule did not name")
        train = [s for k, s in zip(ks, starts) if k >= START_LEAD]
        assert np.all(np.diff(train) == PERIOD), (
            f"consecutive plays are not exactly {PERIOD} slots apart: "
            f"{sorted(set(np.diff(train).tolist()))}")

    def test_the_playout_carries_the_waveform_in_every_scheduled_block_and_nothing_elsewhere(
            self, tb):
        """The schedule is asserted at the **converter**, not only in the responses.

        A response says what the design believed; the RF sink says what came out.  Both are needed:
        a design could report a correct schedule and play at the wrong phase, and it is the phase
        that a drifting grid destroys.
        """
        played = played_samples(tb)
        assert played.size == N_BLK * BLK_SAMP
        want = waveform()
        expect = {_play_block(k) for k in _scheduled_ks()}
        assert _filled(played) == expect, (
            "the blocks that carried data are not the blocks the schedule named")
        for j in expect:
            assert np.array_equal(_blocks(played)[j], want), (
                f"block {j} carries something other than the waveform — the window played at the "
                f"wrong phase, which is what sub-period drift looks like")

    def test_the_startup_hole_is_exactly_the_lead_and_nothing_more(self, tb):
        """The transient, left visible and **asserted** rather than tidied away.

        Block 0 is gone before the first command has crossed the loader.  Blocks 2..4 are the price
        of ``start_now`` on the first play and nothing else: a ``TxResp`` is deferred until its
        window has played, so the host cannot name a slot until a period after the one it learns
        about.  See :class:`TestPrimingClosesTheStartupHole` for what closes it.
        """
        assert tb.dac_if.underrun == 4
        assert _filled(played_samples(tb)) ^ set(range(N_BLK)) == {0, 2, 3, 4}
        assert tb.dac_if.overrun == 0
        assert tb.dac_if.blocks_sent == tb.dac_if.blocks_delivered == N_BLK

    def test_the_counters_agree_with_the_playout(self, tb):
        c = tb.dut.counters
        assert c["n_admitted"] == c["n_transmitted"] == 37
        assert c["n_too_late"] == c["n_missed"] == 0
        assert c["n_no_slot"] == c["n_misaligned"] == c["n_zero_len"] == 0
        assert c["n_status_dropped"] == 0
        assert c["n_blocks_played"] == 37
        assert c["played_through"] == tb.host.base + (NREPEAT - 1) * PERIOD + NSAMP - 1
        tb.dut.assert_clean()


class TestPrimingClosesTheStartupHole:
    """The measurement behind the one host-side constant in this example.

    ``start_now`` on the first play and nothing else leaves a hole between that window and the first
    absolutely-scheduled one, and a hole in the *middle* of a run is not a startup transient — which
    is why :meth:`~waveflow.hw.rf_sample_if.RFSampIF.assert_clean` cannot be used on it.  Priming the
    hole with further ``now`` windows — which the plan says land on consecutive slots for free —
    makes the playout contiguous and ``assert_clean`` applicable.  Both are run so the difference is
    evidence rather than an argument.
    """

    def test_one_now_window_leaves_a_hole_that_assert_clean_cannot_describe(self):
        tb = run_pysim(prime_now=1)
        assert tb.dac_if.underrun == 4
        assert tb.dac_if.last_underrun_idx == 5, "the last empty block is past the leading run"
        with pytest.raises(AssertionError, match="past the .* startup transient"):
            tb.dac_if.assert_clean(4)

    def test_priming_the_hole_makes_the_playout_contiguous(self):
        tb = run_pysim(prime_now=START_LEAD)
        assert _filled(played_samples(tb)) == set(range(FIRST_PLAY_BLK, N_BLK)), (
            "every block from the first play onward carries data")
        assert tb.dac_if.underrun == 1
        # The gate the plan asks for, now that it applies: underrun == n EXACTLY and never after
        # block n.  Strictly stronger than `== 0`, which passes a design that recovers by accident.
        tb.dac_if.assert_clean(FIRST_PLAY_BLK)
        assert tb.dut.counters["n_transmitted"] == NREPEAT == 40


# ---------------------------------------------------------------------------
# 2.  TX_TOO_LATE, driven off zero
# ---------------------------------------------------------------------------


LATE_TID = 10


class TestTooLateIsDrivenOffZero:
    """One window aimed into the past — and the *other* half of the test is the real one.

    That the late window comes back ``TX_TOO_LATE`` shows the player detects lateness.  That **every
    other tid still comes back with its own slot** shows the token/verdict correspondence survived a
    window that resolved out of the ordinary path — which is the failure that would otherwise be
    silent, because a ``tid`` paired with the wrong window's verdict looks exactly like a verdict.
    """

    @pytest.fixture(scope="class")
    def tb(self):
        return run_pysim(prime_now=START_LEAD, late_tid=LATE_TID)

    def test_the_late_window_comes_back_too_late_on_its_own_tid(self, tb):
        bad = [r for r in tb.host.resps if r[1] != TX_TRANSMITTED]
        assert len(bad) == 1, f"exactly one window was aimed into the past, got {bad}"
        tid, status, start = bad[0]
        assert tid == LATE_TID
        assert status == TX_TOO_LATE
        assert start == tb.host.base + LATE_TID * PERIOD - 2 * PERIOD, (
            "the response reports the slot the command named, which is the one that had gone")

    def test_the_verdict_came_from_the_player_and_nowhere_else(self, tb):
        """There is **one** lateness detector in this design and it is the player.

        The loader has no pre-check by design — a second detector fed by a staler view would be a
        second source of truth for one condition — so ``n_too_late`` on the loader must be exactly
        the ``n_missed`` the player recorded.
        """
        c = tb.dut.counters
        assert c["n_missed"] == 1
        assert c["n_too_late"] == 1
        assert c["n_admitted"] == NREPEAT, "a doomed window is still ADMITTED; nothing pre-refuses it"
        assert c["n_transmitted"] == NREPEAT - 1

    def test_every_other_tid_is_unaffected(self, tb):
        """The positional pairing, asserted rather than assumed.

        Statuses are matched to pending windows **in order**, with no id on the wire.  A window that
        resolves as ``MISSED`` still consumes exactly one pending slot and exactly one status, so
        every ``tid`` after it must still line up with its own schedule.  One off-by-one here would
        shift every later response by one period — and every one of them would still look valid.
        """
        ok = {r[0]: r[2] for r in tb.host.resps if r[1] == TX_TRANSMITTED}
        assert set(ok) == _scheduled_ks(prime_now=START_LEAD) - {LATE_TID}
        for k, start in ok.items():
            assert start == tb.host.base + k * PERIOD, (
                f"tid {k} reports slot {start}, not {tb.host.base + k * PERIOD} — the token/verdict "
                f"correspondence slipped past the refused window")

    def test_the_late_windows_block_is_the_only_one_missing_from_the_playout(self, tb):
        played = played_samples(tb)
        assert _filled(played) == set(range(FIRST_PLAY_BLK, N_BLK)) - {_play_block(LATE_TID)}
        assert tb.dac_if.underrun == 2, "block 0, plus the block whose window never arrived in time"


# ---------------------------------------------------------------------------
# 3.  n_underrun off zero, and recovery on the ORIGINAL grid
# ---------------------------------------------------------------------------


STARVE_FROM, STARVE_PLAYS = 12, 4


class TestUnderrunRecoversOnTheGrid:
    """**The assertion this example exists for.**

    Starving the host is easy; what is worth proving is where the playout resumes.  A design that
    recovered by re-basing its schedule on the gap would still be periodic, still report a clean run,
    and still be wrong — every sample after the gap would be in the wrong place forever.  So the
    check is not "it recovered" but "it resumed **at** ``base + k*PERIOD``", asserted at the
    converter.
    """

    @pytest.fixture(scope="class")
    def tb(self):
        return run_pysim(prime_now=START_LEAD, starve_from=STARVE_FROM, starve_plays=STARVE_PLAYS)

    def test_the_gap_is_exactly_the_starved_plays(self, tb):
        """``n_underrun`` driven off zero, and by a known amount rather than merely non-zero."""
        gap = {_play_block(k) for k in range(STARVE_FROM, STARVE_FROM + STARVE_PLAYS)}
        assert _filled(played_samples(tb)) == set(range(FIRST_PLAY_BLK, N_BLK)) - gap
        assert tb.dac_if.underrun == 1 + STARVE_PLAYS
        assert tb.dac_if.last_underrun_idx == max(gap) + 1, (
            "1-based grid index of the last empty block — the end of the gap, not the end of the run")

    def test_the_schedule_resumes_on_the_original_grid(self, tb):
        """Every play after the gap is at ``base + k*PERIOD`` for **its own** ``k``.

        Not ``previous + PERIOD``, which a re-based design would also satisfy: the ``k`` that the
        host skipped are simply absent, and the ones after them keep their original slots.  That is
        only checkable because the origin was established once and never touched again.
        """
        starved = range(STARVE_FROM, STARVE_FROM + STARVE_PLAYS)
        got = {r[0]: r[2] for r in tb.host.resps}
        assert set(got) == _scheduled_ks(prime_now=START_LEAD, starve=starved)
        for k, start in got.items():
            assert start == tb.host.base + k * PERIOD
        after = sorted(k for k in got if k > max(starved))
        assert after[0] == max(starved) + 1
        assert got[after[0]] == tb.host.base + after[0] * PERIOD, (
            "the first play after the gap is on the original grid; a design that re-based would "
            "have put it one gap-length later and still looked periodic")

    def test_nothing_was_late_and_nothing_was_refused(self, tb):
        """Recovery must cost nothing beyond the gap itself.

        A design that re-based, or that let its slot counter drift while idle, would report the
        plays after the gap as ``TX_TOO_LATE`` — so a zero here is evidence, given the gap above is
        non-zero.
        """
        c = tb.dut.counters
        assert c["n_too_late"] == c["n_missed"] == 0
        assert c["n_no_slot"] == 0
        assert c["n_admitted"] == c["n_transmitted"] == NREPEAT - STARVE_PLAYS == 36
        assert c["n_status_dropped"] == 0
        assert all(r[1] == TX_TRANSMITTED for r in tb.host.resps)

    def test_the_waveform_after_the_gap_is_at_the_right_phase(self, tb):
        """The converter's own account of the same claim.

        A slot-counter that advanced by the wrong amount while idle would resume at the right
        *cadence* and the wrong *phase*, which the responses alone cannot see — they report what the
        design believed.
        """
        blocks = _blocks(played_samples(tb))
        want = waveform()
        for k in range(STARVE_FROM + STARVE_PLAYS, NREPEAT):
            assert np.array_equal(blocks[_play_block(k)], want), (
                f"play {k} resumed at the wrong phase")


# ---------------------------------------------------------------------------
# The remaining counters — a counter that has never counted is not evidence
# ---------------------------------------------------------------------------


class TestTheRefusalsAreReachable:

    def test_a_host_that_ignores_the_admission_condition_is_refused_not_stalled(self):
        """``TX_NO_SLOT`` driven off zero — and the design keeps working around it.

        The whole claim of ``can_write_frame()`` is that a full ``pending`` FIFO **refuses** rather
        than blocking or silently accepting.  So the interesting number is not the 30 refusals; it is
        that every one of the 10 admitted windows still resolved, in order, with no status dropped.
        """
        tb = run_pysim(prime_now=START_LEAD, overrun_in_flight=True)
        c = tb.dut.counters
        assert c["n_no_slot"] > 0
        assert c["n_admitted"] + c["n_no_slot"] == NREPEAT
        assert c["n_transmitted"] == c["n_admitted"], "every admitted window still resolved"
        assert c["n_status_dropped"] == 0
        assert c["n_too_late"] == 0, "a refusal is immediate; it never becomes a lateness"
        refused = {r[0] for r in tb.host.resps if r[1] == TX_NO_SLOT}
        played = {r[0] for r in tb.host.resps if r[1] == TX_TRANSMITTED}
        assert refused & played == set(), "a tid is answered exactly once, one way or the other"
        assert len(refused) + len(played) == NREPEAT
        tb.dut.assert_clean()

    def test_a_zero_length_command_is_refused_and_leaks_nothing(self):
        """The plan's open question, closed — and the closure is checked where it would have hurt.

        Admitting ``nsamp == 0`` leaks a ``pending`` slot **for the rest of the run**, so the
        evidence that refusing it works is not the refusal itself but the 40 windows that resolve
        normally behind it.  Four leaked slots and every later command would come back
        ``TX_NO_SLOT``, for reasons that look nothing like the cause.
        """
        tb = run_pysim(prime_now=START_LEAD, probe_zero_len=True)
        c = tb.dut.counters
        assert c["n_zero_len"] == 1
        assert [r for r in tb.host.resps if r[1] == TX_ZERO_LEN] == [(900, TX_ZERO_LEN, 0)]
        assert c["n_no_slot"] == 0, "the refused command took no pending slot with it"
        assert c["n_admitted"] == c["n_transmitted"] == NREPEAT
        assert tb.dac_if.underrun == 1, "and it cost the playout nothing"

    def test_a_misaligned_window_is_refused(self):
        """``TX_MISALIGNED`` driven off zero.

        Unreachable in the example itself, which runs one sample per word — at that geometry every
        window is trivially aligned.  So it is driven here, on a loader built at
        ``samp_per_word=4``, which is the configuration where the check means anything.  Both halves
        are exercised: a window that *ends* mid-word and one that *starts* mid-word, because only the
        first of those is obvious.
        """
        from waveflow.hw.clock import Clock
        from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
        from waveflow.hw.reverse_stream import AckedStreamIF, AckedStreamSlaveIF
        from waveflow.hw.rf_samp_buf import pack_samples
        from waveflow.hw.rf_tx_stream import TAG_BW, TxCmd, TxLoader, TxResp
        from waveflow.simulation.simulation import Simulation

        sim, clk = Simulation(), Clock(freq=250e6)
        w, spw = 64, 4
        ld = TxLoader(name="ld", sim=sim, bitwidth=w, samp_per_word=spw, clk=clk)
        peer = AckedStreamSlaveIF(name="peer", sim=sim, bitwidth=TAG_BW, slot_period=1e-8)
        link = AckedStreamIF(name="link", sim=sim, clk=clk, bitwidth=TAG_BW, depth=256,
                             ack_depth=ld.max_in_flight)
        link.bind("master", ld.to_player)
        link.bind("slave", peer)

        drv_cmd = StreamIFMaster(name="dc", sim=sim, bitwidth=w, has_tlast=True)
        drv_samp = StreamIFMaster(name="ds", sim=sim, bitwidth=w, has_tlast=True)
        snk = StreamIFSlave(name="sr", sim=sim, bitwidth=w, has_tlast=True)
        for nm, m, s in (("c", drv_cmd, ld.cmd_in), ("s", drv_samp, ld.samp_in),
                         ("r", ld.resp_out, snk)):
            i = StreamIF(name=f"i{nm}", sim=sim, clk=clk, bitwidth=w, depth=256)
            i.bind("master", m)
            i.bind("slave", s)

        got: list[tuple[int, int]] = []
        # (tid, samp_start, nsamp, expected) -- a ragged END, a ragged START, and a clean control.
        cases = [(1, 0, 6, TX_MISALIGNED), (2, 2, 8, TX_MISALIGNED), (3, 0, 8, None)]

        def host():
            for tid, start, nsamp, _ in cases:
                c = TxCmd()
                c.tid, c.samp_start, c.start_now, c.nsamp = tid, start, 0, nsamp
                yield from drv_cmd.write(c)
                npay = (nsamp + spw - 1) // spw
                yield from drv_samp.write(
                    pack_samples(np.arange(npay * spw, dtype=np.uint64), w, spw))
            for _ in range(2):
                r = yield from snk.get_schema(TxResp)
                got.append((int(r.tid), int(r.status)))

        sim.env.process(host())
        sim.env.process(ld._run_iter_forever())
        sim.env.run(until=2e-5)

        assert got == [(1, TX_MISALIGNED), (2, TX_MISALIGNED)], (
            "a window that is not a whole number of words, and one that does not start on a word "
            "boundary, are both refused — and the aligned control is not")
        assert ld.n_misaligned == 2
        assert ld.n_admitted == 1, "the control was admitted"
        # The refused windows drained their payload, so the control's samples were its own.
        assert ld.to_player.n_pending == 1
