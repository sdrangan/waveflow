"""Continuous capture: fill one half while a reader drains the other, and lose nothing.

``plans/t2p_lock_chan.md`` S2, checkpoint 1 — the lock's **second** consumer, and the direction the
region parameter was built for.  S1 proved a handover; this proves two regions, which is the only
dimension S1 left unverified.

**The clean run and the dirty run are the same graph with one knob moved.**
:attr:`~waveflow.hw.rf_pingpong_rx.PingPongWindow.stall_blocks` is how long the reader sits on its
window before releasing it, and it is the only thing on RX that loses samples: you cannot
back-pressure an ADC, so a reader holding the region the capture needs is not a gap, it is capture
that no longer exists.  A gate that could not produce that condition could not tell a design that
keeps up from one that was never pushed.

**Contiguity is the real assertion, not the counter.**  The source is a ramp, so a dropped block is a
*step* in the numbers — visible whether or not anything counted it.  The counter is asserted too,
because the two agreeing is what says the design knows what it lost.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.bram import word_element
from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.locked_mem import LOCK_ACQUIRE
from waveflow.hw.rf_pingpong_rx import (
    CAP_LOST,
    CAP_OK,
    CAP_STATUS_NAMES,
    CAPTURE_SCHEMA_CLASSES,
    DROP_BW,
    N_REGION,
    CaptureWindowHdr,
    PingPongCapture,
    RfPingPongRx,
    split_windows,
)
from waveflow.simulation.simulation import Simulation

WORD_BW = 64
DEPTH = 64
BLK_WORDS = 4
REGION = DEPTH // N_REGION
#: Words per second the "ADC" presents.  One word every 250 ns, so a region is 8 us and a run of a
#: few regions is a handful of firings rather than a thousand.
SRC_WORD_RATE = 4e6
BLK_PERIOD = BLK_WORDS / SRC_WORD_RATE

#: The re-layout does nothing in these tests and is not in the graph: what is on trial is the
#: **capture pair**, and a conversion stage in the middle would mean a failure could be its.  The
#: composite's own wiring is exercised by the structural tests at the bottom and by the codegen gate.
SHIFT = 2


class Bench:
    """A ramp source, the capture pair, and a sink — no converter and no re-layout.

    The source is a plain ``StreamIFMaster`` paced at :data:`SRC_WORD_RATE`, which is the whole of
    what an ADC is for this purpose: something that presents a block on a schedule and cannot be told
    to wait.  Wiring a real ``Rfdc`` in would add a second thing that can fail while proving nothing
    extra about the handover; the example (checkpoint 4) does that.
    """

    def __init__(self, *, stall_blocks: int = 0, depth: int = DEPTH) -> None:
        self.sim = Simulation()
        self.clk = Clock(name="clk", freq=250e6)
        self.dut = RfPingPongRx(sim=self.sim, name="rx", bitwidth=WORD_BW, samp_per_word=4,
                               depth=depth, shift=SHIFT, blk_words=BLK_WORDS,
                               stall_blocks=int(stall_blocks), blk_period=BLK_PERIOD,
                               clk=self.clk)
        # The source drives the CAPTURE directly: the re-layout is a pass-through for this gate and
        # putting it in would make a failure ambiguous.  It stays wired in the composite, and the
        # structural tests below assert it is.
        self.src = StreamIFMaster(sim=self.sim, name="src", bitwidth=WORD_BW, has_tlast=True)
        self.snk = StreamIFSlave(sim=self.sim, name="snk", bitwidth=WORD_BW, has_tlast=True)
        self.cap_if = StreamIF(name="tb_src", sim=self.sim, clk=self.clk, bitwidth=WORD_BW, depth=2)
        self.cap_if.bind("master", self.src)
        self.cap_if.bind("slave", self.dut.capture.samp_in)
        self.out_if = StreamIF(name="tb_out", sim=self.sim, clk=self.clk, bitwidth=WORD_BW, depth=2)
        self.out_if.bind("master", self.dut.w_out)
        self.out_if.bind("slave", self.snk)
        #: Whole frames off the wire — header **and** samples, exactly as a host would see them.
        self.frames: list[np.ndarray] = []

    @property
    def windows(self) -> list[np.ndarray]:
        """Just the samples, header stripped by the schema that defines the layout."""
        return [s for _h, s in split_windows(self.frames, WORD_BW)]

    @property
    def hdrs(self) -> list[CaptureWindowHdr]:
        """Just the headers, in arrival order."""
        return [h for h, _s in split_windows(self.frames, WORD_BW)]

    def _source(self, n_blocks: int):
        """A ramp, one block per block-period, on an **absolute** grid.

        Absolute rather than a relative timeout for the reason every metronome in this repo is: a
        relative wait restarts from wherever ``now`` happens to be, so everything the body yielded
        for is added to the period and never given back.
        """
        t0 = self.src.now
        for i in range(int(n_blocks)):
            blk = np.arange(i * BLK_WORDS, (i + 1) * BLK_WORDS, dtype=np.uint64)
            yield from self.src.write(blk)
            deadline = t0 + (i + 1) * BLK_PERIOD
            yield self.src.timeout(max(0.0, deadline - self.src.now))

    def _drain(self):
        while True:
            self.frames.append(np.asarray((yield from self.snk.get())).ravel().copy())

    def run(self, n_blocks: int, until: float) -> None:
        """Push *n_blocks* of ramp and let the design run to *until*.

        ``until`` is a testbench constant, not a latency: the capture is a free-running consumer that
        never exhausts — that is what continuous capture means — so an unbounded ``env.run()`` would
        not return.
        """
        sim = self.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            p = obj.run_proc()
            if p is not None:
                sim.env.process(p)
        sim.env.process(self._source(n_blocks))
        sim.env.process(self._drain())
        try:
            sim.env.run(until=float(until))
        except Exception:
            for obj in sim._sim_objs:
                obj.error_cleanup()
            raise
        for obj in sim._sim_objs:
            obj.post_sim()


def run_capture(*, stall_blocks: int = 0, n_blocks: int = 40) -> Bench:
    """Push *n_blocks* of ramp through the pair and stop a little after the source does."""
    b = Bench(stall_blocks=stall_blocks)
    b.run(n_blocks, until=(n_blocks + 8) * BLK_PERIOD)
    return b


# ---------------------------------------------------------------------------
# The clean run — the gate this checkpoint exists for
# ---------------------------------------------------------------------------

def test_a_full_swap_cycle_loses_nothing():
    """**The gate.**  The capture fills one half while the reader drains the other, and no sample
    is lost across any number of swaps.

    Three claims, and the third is the one no counter gives: the windows *alternate* between the two
    halves (a design handing the same half out twice moves the right number of words and exercises no
    second region), the drop count is zero, and the drained windows **concatenate into a contiguous
    ramp** — which is the same statement made from the samples, where a counter cannot be believed.
    """
    b = run_capture()
    dut = b.dut
    dut.assert_ran(min_windows=2)
    dut.assert_no_loss()
    flat = dut.assert_windows_contiguous(b.windows, where="clean: ")
    assert flat.size == len(b.windows) * REGION
    assert int(flat[0]) == 0, (
        f"the first drained window starts at sample {int(flat[0])}, not 0 — the capture handed out "
        f"a region before it had filled it from the beginning")


def test_the_windows_are_whole_regions_and_arrive_as_frames():
    """Each window is one region and one frame.

    A short window is the failure a word count hides: the samples in it are all correct, there are
    just fewer of them, and the next window's first sample is still the right number because the
    capture kept going.  The frame boundary is what lets a host know where one ends without being
    told a length.
    """
    b = run_capture()
    assert b.frames, "no window reached the sink"
    hn = CaptureWindowHdr.nwords_per_inst(WORD_BW)
    assert {int(f.size) for f in b.frames} == {hn + REGION}, (
        f"frames of {sorted({int(f.size) for f in b.frames})} words, expected only "
        f"{hn + REGION} — one header word and {REGION} samples")
    assert {int(w.size) for w in b.windows} == {REGION}


def test_the_capture_never_stalls_its_input():
    """Every block the source presented was taken off the wire, dropped or not.

    A body that read only when it had room would be back-pressuring an ADC, which is not a thing that
    can happen — and it would make the drop counter read zero for a design that was quietly losing
    everything upstream instead.
    """
    n_blocks = 40
    b = Bench()
    b.run(n_blocks, until=(n_blocks + 8) * BLK_PERIOD)
    assert int(b.dut.capture.n_blocks) == n_blocks, (
        f"the capture took {int(b.dut.capture.n_blocks)} of {n_blocks} blocks off its input. The "
        f"rest are still queued, which means the design stalled the source — the one thing an RX "
        f"front end may not do.")
    assert int(b.dut.capture.n_written) + int(b.dut.capture.n_dropped) == n_blocks * BLK_WORDS


# ---------------------------------------------------------------------------
# The dirty run — a clean pass above means nothing without it
# ---------------------------------------------------------------------------

def test_a_reader_that_holds_its_window_too_long_loses_samples():
    """**The paired dirty run**, and it is the same graph with one knob moved.

    A reader that sits on its window for longer than the capture takes to fill the other region is
    the only thing that loses samples on RX.  What comes out is still a perfectly good ramp in every
    window — the loss is a *step between* windows, which is why the contiguity check is the assertion
    and the counter is the corroboration.
    """
    b = run_capture(stall_blocks=REGION // BLK_WORDS + 2)
    dut = b.dut

    assert dut.n_dropped > 0, (
        "the stalled reader lost nothing, so this run does not distinguish a design that keeps up "
        "from one that was never pushed — and the clean result above is not evidence of anything")
    with pytest.raises(AssertionError, match="dropped"):
        dut.assert_no_loss()
    with pytest.raises(AssertionError, match="not contiguous"):
        dut.assert_windows_contiguous(b.windows, where="dirty: ")

    # THE TWO READINGS OF THE SAME LOSS, AND WHY THEY ARE NOT THE SAME NUMBER.
    #
    # The samples can only show loss that fell BETWEEN two windows the reader actually drained.  The
    # counter also holds whatever was dropped after the last one -- the run ends with the reader
    # still holding a window, and the capture goes on losing blocks it will never get to announce.
    # So the relation is a BOUND, and asserting equality would be asserting that the run stopped at
    # a convenient moment.  MEASURED here: 16 words visible against 32 counted, the difference being
    # four blocks that fell past the last drained window.
    flat = np.concatenate([np.asarray(w).reshape(-1) for w in b.windows]).astype(np.int64)
    step = np.diff(flat)
    gaps = step[step != 1] - 1
    visible = int(gaps.sum())
    assert 0 < visible <= dut.n_dropped, (
        f"the samples show {visible} word(s) missing between drained windows and the counter says "
        f"{dut.n_dropped}. Visible loss must be non-zero (or the dirty run proved nothing) and can "
        f"never exceed the count (or the design is losing samples it does not know about).")
    assert all(int(g) % BLK_WORDS == 0 for g in gaps), (
        f"the visible gaps are {[int(g) for g in gaps]} words, and every one should be a whole "
        f"number of {BLK_WORDS}-word blocks: the capture drops a BLOCK at a time, so a partial gap "
        f"would mean something other than the drop path lost those samples.")


def test_every_window_is_still_full_and_valid_when_samples_were_lost():
    """The shape of the failure, stated so a reader of the gate knows what to look for.

    Loss on RX does not truncate a window and does not corrupt one.  Every window is the right length
    and every sample in it is a real sample — the capture simply never saw the ones in between.  That
    is why it is silent, and why the gate has to look *across* windows rather than inside them.
    """
    b = run_capture(stall_blocks=REGION // BLK_WORDS + 2)
    assert b.dut.n_dropped > 0
    assert {int(w.size) for w in b.windows} == {REGION}
    for w in b.windows:
        assert np.array_equal(np.diff(np.asarray(w).astype(np.int64)), np.ones(REGION - 1)), (
            "a window is internally non-contiguous; the loss is supposed to fall BETWEEN windows")


# ---------------------------------------------------------------------------
# The ordering the protocol turns on, from the other side
# ---------------------------------------------------------------------------

def test_the_capture_never_grants_a_region_it_is_filling():
    """On RX the ordering rule costs nothing, and the guard still has to be there.

    TX needs a state change before the grant.  Here the reader only ever asks for a region the
    capture has already announced and moved off, so the grant is free — but a design that drifted
    into granting the half it is filling would corrupt a window silently, so the interface's guard is
    what catches it.  This drives that case directly.
    """
    b = Bench()
    cap = b.dut.capture

    def grant_the_live_region():
        # Fill part of region 0, then hand region 0 out while still pointed at it.
        yield from cap.lock.write_pipelined(np.arange(BLK_WORDS, dtype=np.uint64), addr=0)
        yield from cap.lock.grant(0, REGION)
        yield from cap.lock.write_pipelined(np.arange(BLK_WORDS, dtype=np.uint64), addr=BLK_WORDS)

    sim = b.sim
    for obj in sim._sim_objs:
        obj.pre_sim()
    for obj in sim._sim_objs:
        p = obj.run_proc()
        if p is not None:
            sim.env.process(p)
    sim.env.process(grant_the_live_region())
    with pytest.raises(RuntimeError, match="has YIELDED"):
        sim.env.run(until=10 * BLK_PERIOD)


def test_a_region_is_free_only_when_it_is_RELEASED_not_when_it_is_taken():
    """The *full* flag is cleared by the release, and that is what makes the drop condition real.

    If taking a region freed it, the capture could refill the half a reader is still draining — which
    is the collision, arrived at from the bookkeeping rather than from the lock.  So a region stays
    unavailable from the moment it is filled until the moment it comes back.
    """
    b = Bench()
    cap = b.dut.capture
    assert cap.full == [False] * N_REGION
    cap.full[0] = True
    assert cap._free_region() == 1, "the second region should still be available"
    cap.full[1] = True
    assert cap._free_region() is None, (
        "both regions hold unread samples, so there is nowhere to put a block — that is the drop "
        "condition, and it must not be reachable by any other route")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_the_capture_is_the_OWNER_and_it_WRITES():
    """The RX inversion, asserted where a reader of the design will look for it.

    The side that cannot stop is the owner in both directions; on RX that side is the one *filling*
    the memory.  So the owner writes and the requester reads — the inverse of TX — and the interface
    routes each to the memory port its direction needs rather than the one its role would have got.
    """
    b = Bench()
    dut = b.dut
    assert dut.capture.lock.access == "write" and dut.window.lock.access == "read"
    assert dut.capture.lock.mem_ep.interface is dut.lock.wr_if
    assert dut.window.lock.mem_ep.interface is dut.lock.rd_if
    # ... and port A is still the writer's, because bram_t2p.v's $error is one-sided.
    assert dut.lock.wr_if.endpoints["slave"] is dut.mem.wr_port


def test_the_rdy_channel_is_as_deep_as_the_regions_are_many():
    """The invariant that lets the capture write it **blocking** without ever stalling.

    At most :data:`~waveflow.hw.rf_pingpong_rx.N_REGION` regions can be full at once, so at most that
    many announcements can be outstanding.  A shallower channel would make the capture block on a
    reader — which is back-pressuring an ADC, the one thing this design may not do.
    """
    b = Bench()
    rdy = b.dut.interfaces["rx_rdy_if"]
    assert rdy.depth == N_REGION
    assert rdy.endpoints["master"] is b.dut.capture.rdy_out
    assert rdy.endpoints["slave"] is b.dut.window.rdy_in


def test_the_relayout_is_first_and_is_not_the_identity():
    """Dense in the memory on both sides, so the conversion moves to whichever end faces the
    converter — and a build with ``shift == 0`` would be measuring a pair of wires."""
    b = Bench()
    assert not b.dut.is_identity
    assert b.dut.samp_in is b.dut.relayout.s_in
    dense = b.dut.interfaces["rx_dense_if"]
    assert dense.endpoints["master"] is b.dut.relayout.s_out
    assert dense.endpoints["slave"] is b.dut.capture.samp_in


@pytest.mark.parametrize("kw, match", [
    ({"depth": 96}, "power of two"),
    ({"blk_words": 3}, "does not divide"),
])
def test_a_geometry_that_cannot_split_into_regions_is_refused(kw, match):
    """Checked at construction, where the message can still name the numbers.

    A region that is not a whole number of blocks would put half a block on the far side of a lock
    the capture does not hold, and an odd split would make one window shorter than the other — at
    which point the contiguity check would have to know which.
    """
    with pytest.raises(ValueError, match=match):
        RfPingPongRx(sim=Simulation(), name="bad", bitwidth=WORD_BW, samp_per_word=4,
                     depth=kw.get("depth", DEPTH), shift=SHIFT,
                     blk_words=kw.get("blk_words", BLK_WORDS))


def test_the_capture_polls_exactly_once_per_block():
    """``check_period`` is the block, which is what makes the reader's wait a stated number."""
    b = Bench()
    assert b.dut.capture.lock.check_period == BLK_WORDS
    assert isinstance(b.dut.capture, PingPongCapture)


# ---------------------------------------------------------------------------
# S2 checkpoint 2 — the count and its verdict, on the wire
# ---------------------------------------------------------------------------

def test_the_header_is_one_word_and_the_layout_has_one_author():
    """``8 + 28 + 28 == 64``, and the gate reads it through the schema's own deserializer.

    A test that sliced the header off by hand would be a second author of the layout, and the two
    would be free to disagree about a field width — silently, because both would still produce a
    number.
    """
    assert CAPTURE_SCHEMA_CLASSES == [CaptureWindowHdr]
    assert int(CaptureWindowHdr.get_bitwidth()) == 64
    assert int(CaptureWindowHdr.nwords_per_inst(WORD_BW)) == 1
    assert DROP_BW == 28


def test_a_clean_run_publishes_CAP_OK_and_zero_on_every_window():
    """**The verdict, read off the wire.**

    Off the stream rather than off the counter, for the reason ``rf_shot_unified`` reads its responses
    off the wire: a design that counted correctly and *serialized* wrongly passes every internal
    check there is, and the wire is the only thing a host can act on.
    """
    b = run_capture()
    b.dut.assert_published_loss(b.frames, where="clean: ")
    assert [int(h.status) for h in b.hdrs] == [CAP_OK] * len(b.hdrs)
    assert [int(h.n_dropped) for h in b.hdrs] == [0] * len(b.hdrs)


def test_every_window_says_which_region_it_came_from():
    """The header's ``base_addr`` is what the reader turned into its ``acquire``.

    So the alternation is visible to a **host**, not only to a test reaching into the design — which
    matters because a design handing the same half out twice is otherwise indistinguishable from one
    that swaps.
    """
    b = run_capture()
    assert [int(h.base_addr) for h in b.hdrs] == [(i % N_REGION) * REGION
                                                  for i in range(len(b.hdrs))]
    assert [int(h.base_addr) for h in b.hdrs] == b.dut.window.bases


def test_loss_is_LOUD_on_the_wire_and_not_only_in_a_counter():
    """**The point of checkpoint 2.**  A dropped block changes the header of the very next window.

    Everything else about that window is perfect: it is the right length, every sample in it is a
    real sample, and the numbers inside it are contiguous.  Without the header, the only evidence
    is a Python attribute nobody reads — which is exactly the shape sub-block loss had before
    ``offer()`` published it.
    """
    b = run_capture(stall_blocks=REGION // BLK_WORDS + 2)
    assert b.dut.n_dropped > 0

    with pytest.raises(AssertionError, match="CAP_LOST"):
        b.dut.assert_published_loss(b.frames, where="dirty: ")

    lost = [i for i, h in enumerate(b.hdrs) if int(h.status) == CAP_LOST]
    assert lost, (
        f"nothing on the wire says this run lost samples: statuses "
        f"{[CAP_STATUS_NAMES[int(h.status)] for h in b.hdrs]}")
    # ... and the window carrying the verdict is itself flawless, which is why the header has to say
    # so: the loss fell BEFORE it, not inside it.
    w = b.windows[lost[0]]
    assert int(w.size) == REGION
    assert np.array_equal(np.diff(w.astype(np.int64)), np.ones(REGION - 1))


def test_the_cumulative_count_never_goes_backwards_and_the_last_one_is_the_total():
    """Cumulative, never incremental — :mod:`waveflow.hw.reverse_stream`'s rule 1.

    A lost cumulative value is harmless because the next one carries the whole truth; a lost
    *increment* is wrong forever.  So the sequence must be monotone, and its last member must be what
    the design itself counted — a wire that disagrees with the counter reports a number nobody should
    trust.
    """
    b = run_capture(stall_blocks=REGION // BLK_WORDS + 2)
    counts = [int(h.n_dropped) for h in b.hdrs]
    assert counts == sorted(counts), f"the cumulative drop count went backwards: {counts}"
    # A BOUND, not an equality, and for checkpoint 1's reason: the last window publishes what was
    # known at the last ANNOUNCEMENT, and the capture goes on dropping after it -- the run ends with
    # the reader still holding a window.  MEASURED: 16 published against 32 counted.  Asserting
    # equality would be asserting that the run stopped at a convenient moment.
    assert 0 < counts[-1] <= int(b.dut.capture.n_dropped), (
        f"the last window published {counts[-1]} and the design counted "
        f"{int(b.dut.capture.n_dropped)}: a published total must be non-zero on a lossy run and can "
        f"never exceed what was actually dropped.")


def test_the_verdict_marks_the_window_AFTER_the_gap_and_not_the_one_before():
    """Which window the verdict belongs to, asserted rather than assumed.

    ``CAP_LOST`` means *samples were lost immediately before this window* — so the flagged window is
    the one whose first sample does **not** follow the previous window's last.  Marking the window
    before the gap instead would point a host at data that is entirely fine.
    """
    b = run_capture(stall_blocks=REGION // BLK_WORDS + 2)
    wins, hdrs = b.windows, b.hdrs
    assert len(wins) >= 2
    for i in range(1, len(wins)):
        gap = int(wins[i][0]) - int(wins[i - 1][-1]) - 1
        want = CAP_LOST if gap else CAP_OK
        assert int(hdrs[i].status) == want, (
            f"window {i} follows a gap of {gap} sample(s) and its header says "
            f"{CAP_STATUS_NAMES[int(hdrs[i].status)]}, expected {CAP_STATUS_NAMES[want]}")


def test_the_first_window_is_CAP_OK_because_nothing_precedes_it():
    """There is no gap before the first window, so its verdict is not a special case — it is just
    the general rule with nothing on the left."""
    b = run_capture()
    assert int(b.hdrs[0].status) == CAP_OK and int(b.hdrs[0].n_dropped) == 0
