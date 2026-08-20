"""The two reverse channels — :class:`CreditStreamIF` and :class:`AckedStreamIF` — in pysim.

Stage 0 of ``plans/rf_samp_new.md``.  Five tests carry the stage, and each exists because it covers
a failure nothing currently does:

===========================================  =====================================================
test                                         what it demonstrates
===========================================  =====================================================
``TestCreditToZeroAndBack``                  the accounting debits on write and restores on ack
``TestDroppedReverseValuesSelfHeal``         the cumulative form self-heals (rule 1)
``TestTheCountersWrap``                      masked arithmetic stays exact where unbounded ints do
                                             not — walked **onto** the boundary, not near it
``TestSaturationInvertsTheChannel``          the reader receives the **oldest** values while fresh
                                             ones are discarded (rule 4)
``TestTheAdmissionContractIsAsserted``       ``write_frame`` without ``can_write_frame`` is refused
===========================================  =====================================================

**Every test drives its stream from a single SimPy process.**  Two processes would make the
interleaving a property of SimPy's scheduler rather than of the interface, and the quantities under
test here — how many values were in the reverse FIFO when the reader arrived — are exactly the ones
that would then stop being reproducible.  Where a test needs concurrency it says so.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.reverse_stream import (
    CTR_BITS,
    CTR_MASK,
    MAX_IN_FLIGHT,
    RESP_WORDS,
    AckedStreamIF,
    AckedStreamMasterIF,
    AckedStreamSlaveIF,
    CreditStreamIF,
    CreditStreamMasterIF,
    CreditStreamSlaveIF,
    udiff,
)
from waveflow.simulation.simulation import Simulation

FREQ = 250e6
SLOT = 4e-9


def _w(*vals) -> np.ndarray:
    """A raw word burst."""
    return np.array(vals, dtype=np.uint32)


def _credit(depth=8, credit_depth=4, ctr_bits=CTR_BITS, resp_words=RESP_WORDS, bitwidth=32):
    sim = Simulation()
    m = CreditStreamMasterIF(name="m", sim=sim, bitwidth=bitwidth,
                             resp_words=resp_words, ctr_bits=ctr_bits)
    s = CreditStreamSlaveIF(name="s", sim=sim, bitwidth=bitwidth, ctr_bits=ctr_bits)
    iface = CreditStreamIF(name="c", sim=sim, clk=Clock(freq=FREQ), bitwidth=bitwidth,
                           depth=depth, credit_depth=credit_depth)
    iface.bind("master", m)
    iface.bind("slave", s)
    return sim, iface, m, s


def _acked(depth=64, ack_depth=None, max_in_flight=MAX_IN_FLIGHT, slot_period=SLOT, bitwidth=32):
    sim = Simulation()
    m = AckedStreamMasterIF(name="m", sim=sim, bitwidth=bitwidth, max_in_flight=max_in_flight)
    s = AckedStreamSlaveIF(name="s", sim=sim, bitwidth=bitwidth, slot_period=slot_period)
    iface = AckedStreamIF(name="a", sim=sim, clk=Clock(freq=FREQ), bitwidth=bitwidth,
                          depth=depth, ack_depth=ack_depth)
    iface.bind("master", m)
    iface.bind("slave", s)
    return sim, iface, m, s


def _run(sim, body):
    """Run *body* (a zero-arg generator function) as the only process, to completion."""
    sim.env.process(body())
    sim.env.run()


# ---------------------------------------------------------------------------
# 1.  Credit driven to zero and back
# ---------------------------------------------------------------------------


class TestCreditToZeroAndBack:
    """The accounting debits on write and restores on ack — and the verdict still fits at zero."""

    def test_credit_debits_to_zero_then_restores_word_by_word(self):
        """``avail`` is ``depth - resp_words - outstanding`` at every step, in both directions.

        The half that is easy to get wrong is the *restore*: an implementation that accumulated
        increments instead of taking the newest cumulative total would also pass the debit half.
        """
        sim, iface, m, s = _credit(depth=8, credit_depth=4)
        seen_down, seen_up = [], []

        def body():
            assert m.depth == 8
            assert m.avail == 7, "depth 8 with 1 word reserved for a response"

            for k in range(7):
                assert (yield from m.write_nb(_w(k))) is True
                seen_down.append(m.avail)
            assert m.avail == 0
            assert m.outstanding == 7

            # Zero credit: data is refused, and refused is not the same as stalled.
            assert (yield from m.write_nb(_w(99))) is False
            assert m.n_no_room == 1

            # ...but the verdict still fits, because its headroom was never data's to compete for.
            assert (yield from m.write_resp_nb(_w(0xDEAD))) is True
            assert m.n_resp_no_room == 0
            assert m.outstanding == 8 == m.depth

            for k in range(8):
                yield from s.get(nwords_max=1)
                yield from m.poll_credit(1)
                assert m.acked == k + 1
                seen_up.append(m.avail)

        _run(sim, body)

        assert seen_down == [6, 5, 4, 3, 2, 1, 0], "one word written debits exactly one word"
        assert seen_up == [0, 1, 2, 3, 4, 5, 6, 7], "one word consumed restores exactly one word"
        assert m.outstanding == 0
        assert s.consumed == 8
        assert iface.n_credit_dropped == 0, (
            "a reader polling once per consumer get outpaces the writer — the rate argument rule 4 "
            "leaves the RX side standing on")

    def test_a_burst_larger_than_the_credit_is_refused_whole(self):
        """Refused, not clipped and not routed to the unbounded overflow queue.

        ``_push_to_endpoint`` will happily accept a burst longer than ``depth`` by spilling the tail
        into an unbounded container.  That is right for a plain stream and wrong here: the producer
        asked whether the transaction fits, and half of it fitting is not an answer it can use.
        """
        sim, iface, m, s = _credit(depth=8, credit_depth=4)

        def body():
            assert (yield from m.write_nb(_w(*range(7)))) is True
            assert m.avail == 0
            assert (yield from m.write_nb(_w(0, 1, 2))) is False
            assert m.written == 7, "nothing partial was written"
            assert m.n_no_room == 1

        _run(sim, body)


# ---------------------------------------------------------------------------
# 2.  Reverse values deliberately dropped
# ---------------------------------------------------------------------------


class TestDroppedReverseValuesSelfHeal:
    """Rule 1: a lost reverse value is harmless because the next carries the whole truth."""

    def test_five_of_six_credit_values_never_sent_and_the_sixth_restores_everything(self):
        """The consumer consumes six words and puts **one** value on the wire.

        Dropped on purpose rather than by saturation, so the self-heal is isolated from rule 4: the
        question here is only whether a value that never arrived costs anything permanent.

        The counterfactual is the assertion that matters.  Were the reverse value *incremental*, the
        one that survived would carry ``+1`` and the producer would sit at ``avail == 2`` forever,
        wedged against a channel that is in fact empty.  It carries ``6``.
        """
        sim, iface, m, s = _credit(depth=8, credit_depth=4)
        nvalues = []

        def body():
            for k in range(6):
                assert (yield from m.write_nb(_w(k))) is True
            assert m.avail == 1

            for k in range(6):
                yield from s.fwd_ep.get(nwords_max=1)
                s.consumed = (s.consumed + 1) & CTR_MASK
                if k == 5:
                    yield from s.offer_credit()      # only the LAST value ever reaches the wire

            # A bounded poll wide enough to have taken all six, had six been sent.
            nvalues.append((yield from m.poll_credit(6)))

        _run(sim, body)

        assert nvalues == [1], "five values were genuinely never sent, not merely unread"
        assert m.acked == 6, "the survivor carried the whole total, not the last increment"
        assert m.outstanding == 0
        assert m.avail == 7, "full credit restored from a single surviving value"
        assert m.acked != 1, (
            "an incremental reverse value would leave the producer stuck at avail == 2 here, with "
            "no way ever to recover the five it lost")


# ---------------------------------------------------------------------------
# 3.  The counters, walked onto the wrap
# ---------------------------------------------------------------------------


class TestTheCountersWrap:
    """``ap_uint<N>`` wraps by itself; Python ints do not.  Put the test **at** the boundary."""

    def test_udiff_is_exact_at_the_sixteen_bit_boundary(self):
        """Pure arithmetic, at the real width, on the exact values that straddle it."""
        assert udiff(0x0000, 0xFFFF) == 1
        assert udiff(0x0002, 0xFFFF) == 3
        assert udiff(0xFFFF, 0xFFFF) == 0
        assert udiff(0x0003, 0x0000) == 3
        # The same subtraction on unbounded ints, which is what a careless twin computes.
        assert 0x0002 - 0xFFFF == -65533
        assert udiff(0x0002, 0xFFFF) != 0x0002 - 0xFFFF

    def test_the_accounting_stays_exact_while_both_counters_cross_zero(self):
        """Walk ``written`` and ``acked`` **onto** ``0x00``, with words outstanding at the crossing.

        Run at ``ctr_bits=8`` so the walk is 260 words rather than 65 536; the masking law does not
        depend on the width, and the *boundary* is what is under test.  The 16-bit width itself is
        pinned by :meth:`test_udiff_is_exact_at_the_sixteen_bit_boundary` above.

        Three quantities are recorded at every step of the crossing:

        * ``true`` — words really in the channel, counted by the test on unbounded ints;
        * ``masked`` — what :attr:`CreditStreamMasterIF.outstanding` computes;
        * ``naive`` — an unbounded ``written`` differenced against the value that came off the
          wire, which is *necessarily* masked because the consumer's counter is an ``ap_uint``.

        ``naive`` is the twin this project keeps writing by accident, and it agrees with ``true``
        everywhere until the wire value wraps — which is the shape of every fidelity defect this arc
        has found.
        """
        bits = 8
        mask = (1 << bits) - 1
        sim, iface, m, s = _credit(depth=8, credit_depth=4, ctr_bits=bits)
        trace = []            # (naive_written, true_outstanding, masked, naive)
        acked_seen = []

        def step_record(naive_written, true_out):
            trace.append((naive_written, true_out, m.outstanding, naive_written - m.acked))
            acked_seen.append(m.acked)

        def body():
            naive_written = 0

            # Lockstep to just short of the wrap: written == acked == 253.
            for k in range(253):
                assert (yield from m.write_nb(_w(k & mask))) is True
                naive_written += 1
                yield from s.get(nwords_max=1)
                yield from m.poll_credit(1)
            assert m.written == 253 and m.acked == 253 and m.outstanding == 0

            # Five words written and NOT consumed: ``written`` crosses 0x00 with credit outstanding.
            for i in range(5):
                assert (yield from m.write_nb(_w(i))) is True
                naive_written += 1
                step_record(naive_written, i + 1)
            assert m.written == (258 & mask) == 2

            # Now consume them one at a time: ``acked`` crosses 0x00 too.
            for i in range(5):
                yield from s.get(nwords_max=1)
                yield from m.poll_credit(1)
                step_record(naive_written, 4 - i)

        _run(sim, body)

        assert 0xFF in acked_seen and 0x00 in acked_seen, (
            "the walk must land ON the boundary, not near it — 0xFF and 0x00 both observed")
        for naive_written, true_out, masked, naive in trace:
            assert masked == true_out, (
                f"masked accounting wrong at written={naive_written}: {masked} != {true_out}")
        diverged = [t for t in trace if t[3] != t[1]]
        assert diverged, (
            "the unbounded-int computation must actually break somewhere in this trace, or the "
            "test proves nothing about masking")
        first_bad = diverged[0]
        assert first_bad[0] >= 256, "divergence starts once a wire value has wrapped, not before"
        assert first_bad[3] > first_bad[1] + mask, (
            "and it is not a small error: the naive difference jumps by a whole modulus")
        assert m.outstanding == 0
        assert s.consumed == (258 & mask)


# ---------------------------------------------------------------------------
# 4.  Saturation
# ---------------------------------------------------------------------------


class TestSaturationInvertsTheChannel:
    """Rule 4: saturate the reverse FIFO and "the newest supersedes" **inverts**.

    The failure is not staleness that a later value fixes — it is that the reader receives ancient
    values forever while every fresh one is discarded.  A test that never actually fills the queue
    passes while proving nothing, so the queue level is asserted at capacity before anything is read.
    """

    def test_the_reader_gets_the_oldest_values_and_the_fresh_ones_are_gone(self):
        sim, iface, m, s = _credit(depth=16, credit_depth=4)
        at_capacity = {}
        taken = []

        def body():
            for k in range(8):
                assert (yield from m.write_nb(_w(k))) is True
            assert m.written == 8

            # Eight consumer gets, eight credit offers, and NOT ONE poll: the writer outpaces the
            # reader, which is the only condition under which rule 4 bites.
            for _ in range(8):
                yield from s.get(nwords_max=1)

            at_capacity["level"] = m.crd_ep.nrx.level
            at_capacity["capacity"] = m.crd_ep.nrx.capacity

            # A bounded poll wide enough for all eight, had all eight survived.
            for _ in range(8):
                n = yield from m.poll_credit(1)
                if n == 0:
                    break
                taken.append(m.acked)

        _run(sim, body)

        assert at_capacity["capacity"] == 4
        assert at_capacity["level"] == 4, (
            "the reverse FIFO must actually be FULL — a saturation test that never saturates is the "
            "easiest one in this file to write vacuously")
        assert iface.n_credit_dropped == 4, "offers 5..8 had nowhere to go"

        assert taken == [1, 2, 3, 4], (
            "the reader receives the OLDEST four values, in order — not the newest, and not the "
            "current total")
        assert s.consumed == 8, "the truth at the consumer had moved on to 8"
        assert m.acked == 4, "the producer's view is stuck four words in the past, permanently"

        # The producer is now permanently pessimistic: it believes 4 words are still in a channel
        # that is empty, and no later value will ever tell it otherwise.
        assert m.outstanding == 4
        assert m.avail == 16 - RESP_WORDS - 4
        assert m.avail < 16 - RESP_WORDS

    def test_the_ack_channel_refuses_to_be_built_where_this_could_happen(self):
        """The TX side answers rule 4 **structurally**, and the check is at bind time.

        A dropped status is not a stale value that self-heals — it mis-pairs every later token — so
        the sizing rule is enforced rather than argued.  This is the difference the plan draws
        between the two channels, made mechanical.
        """
        sim = Simulation()
        m = AckedStreamMasterIF(name="m", sim=sim, bitwidth=32, max_in_flight=8)
        iface = AckedStreamIF(name="a", sim=sim, clk=Clock(freq=FREQ), bitwidth=32,
                              depth=64, ack_depth=4)
        with pytest.raises(ValueError, match="shallower than max_in_flight"):
            iface.bind("master", m)

    def test_a_correctly_sized_ack_channel_never_drops(self):
        """The other half: the sizing rule is satisfiable, so ``n_status_dropped == 0`` is a gate
        that can actually be met rather than an unreachable ideal."""
        sim, iface, m, s = _acked(max_in_flight=4, ack_depth=4)
        resolved = []

        def body():
            for f in range(4):
                assert m.can_write_frame()
                yield from m.write_frame(_w(*range(f * 10, f * 10 + 3)), token=f"f{f}")
            assert m.n_pending == 4
            # Consume all four frames and answer every mark before the producer harvests anything —
            # four statuses into a depth-4 FIFO with no reader is exactly the sizing bound.
            for _ in range(4):
                fr = yield from s.read_frame_nb()
                assert fr is not None
                for it in fr:
                    if it.mark:
                        yield from s.send_status(it.item)
            assert m.n_status_dropped == 0
            resolved.extend((yield from m.harvest(4)))

        _run(sim, body)

        assert resolved == [("f0", 2), ("f1", 12), ("f2", 22), ("f3", 32)]
        m.assert_clean()


# ---------------------------------------------------------------------------
# 5.  The admission contract
# ---------------------------------------------------------------------------


class TestTheAdmissionContractIsAsserted:
    """``can_write_frame()`` is an admission condition, and the twin refuses rather than trusts."""

    def test_write_frame_with_no_free_pending_slot_raises(self):
        """Refused, not silently accepted.

        The alternative failure is invisible: a frame written without a pending slot resolves
        against some *other* frame's status, and a token paired with the wrong verdict looks exactly
        like a verdict.
        """
        sim, iface, m, s = _acked(max_in_flight=2)
        caught = {}

        def body():
            yield from m.write_frame(_w(1, 2, 3), token="a")
            yield from m.write_frame(_w(4, 5, 6), token="b")
            assert m.can_write_frame() is False
            with pytest.raises(RuntimeError, match="no free pending slot"):
                yield from m.write_frame(_w(7, 8, 9), token="c")
            caught["pending"] = m.n_pending
            caught["frames"] = m.n_frames

        _run(sim, body)

        assert caught["pending"] == 2, "the refused frame left no trace in the pending FIFO"
        assert caught["frames"] == 2, "and was not counted as written"

    def test_a_slot_frees_when_a_frame_resolves(self):
        """The condition is a condition, not a permanent ceiling — otherwise refusing it would be
        indistinguishable from a wedge."""
        sim, iface, m, s = _acked(max_in_flight=2)

        def body():
            yield from m.write_frame(_w(1, 2, 3), token="a")
            yield from m.write_frame(_w(4, 5, 6), token="b")
            assert m.can_write_frame() is False
            fr = yield from s.read_frame_nb()
            for it in fr:
                if it.mark:
                    yield from s.send_status(it.item)
            assert (yield from m.harvest(2)) == [("a", 3)]
            assert m.can_write_frame() is True
            yield from m.write_frame(_w(7, 8, 9), token="c")

        _run(sim, body)
        assert m.n_pending == 2

    def test_an_empty_frame_is_refused_rather_than_leaking_a_slot(self):
        """``nsamp == 0`` decided explicitly, which is what the plan's open question asks for.

        A zero-length frame has no last item, so no mark is sent, so no status returns and the
        pending slot never pops.  A few of those and the producer refuses everything for reasons
        that look nothing like the cause.
        """
        sim, iface, m, s = _acked(max_in_flight=2)

        def body():
            with pytest.raises(ValueError, match="no last item to mark"):
                yield from m.write_frame(_w(), token="empty")
            assert m.n_pending == 0

        _run(sim, body)


# ---------------------------------------------------------------------------
# Supporting: the ordering guarantee, the per-item reader, and the playout charge
# ---------------------------------------------------------------------------


class TestTokenRecoveryIsPositional:
    """One status per marked item, in the order the marks were sent — no id on the wire."""

    def test_tokens_resolve_in_the_order_their_frames_were_written(self):
        sim, iface, m, s = _acked(max_in_flight=4)
        resolved = []

        def body():
            for f, tok in enumerate(["alpha", "beta", "gamma"]):
                yield from m.write_frame(_w(*range(f * 10, f * 10 + 4)), token=tok)
            for _ in range(3):
                fr = yield from s.read_frame_nb()
                for it in fr:
                    if it.mark:
                        yield from s.send_status(it.item * 2)
            resolved.extend((yield from m.harvest(4)))

        _run(sim, body)

        assert resolved == [("alpha", 6), ("beta", 26), ("gamma", 46)], (
            "the token comes back beside a status computed from ITS OWN frame's last item, with "
            "nothing matched by id")
        assert m.n_pending == 0
        m.assert_clean()

    def test_the_last_item_of_a_frame_is_the_marked_one(self):
        sim, iface, m, s = _acked(max_in_flight=2)
        marks = []

        def body():
            yield from m.write_frame(_w(5, 6, 7, 8), token="t")
            fr = yield from s.read_frame_nb()
            marks.extend(it.mark for it in fr)
            assert [it.item for it in fr] == [5, 6, 7, 8]

        _run(sim, body)
        assert marks == [0, 0, 0, 1], "exactly one mark per frame, on the last item"

    def test_read_nb_hands_out_one_item_at_a_time(self):
        """The per-item shape is the HLS twin's, and it must survive contact with a frame.

        A metronome-paced consumer takes one sample per slot and decides on each; it can never
        consume a frame in one go, so this reader is the one the C++ body is written from.
        """
        sim, iface, m, s = _acked(max_in_flight=2)
        seen = []

        def body():
            assert (yield from s.read_nb()) is None, "non-blocking on an empty channel"
            yield from m.write_frame(_w(1, 2, 3), token="t")
            for _ in range(4):
                r = yield from s.read_nb()
                if r is None:
                    break
                seen.append((r.item, r.mark))
                yield sim.env.timeout(SLOT)          # the caller's own metronome
                if r.mark:
                    yield from s.send_status(r.item)
            assert (yield from m.harvest(2)) == [("t", 3)]

        _run(sim, body)
        assert seen == [(1, 0), (2, 0), (3, 1)]


class TestTheFrameReaderChargesThePlayout:
    """``read_frame_nb`` is the LT approximation, and the one thing that must be right is timing."""

    def test_the_frame_read_costs_one_slot_per_item(self):
        """A frame read that reports immediately hands the producer a verdict *before those items
        would have played*, so the producer runs ahead of what the hardware allows and every rate
        conclusion drawn from the model is optimistic.  That is the correction that made the RX
        ingress twin honest in PR #160 (0 dropped reported against the hardware's 1695).
        """
        sim, iface, m, s = _acked(max_in_flight=2, slot_period=SLOT)
        times = {}

        def body():
            yield from m.write_frame(_w(1, 2, 3, 4, 5), token="t")
            times["before"] = float(sim.env.now)
            fr = yield from s.read_frame_nb()
            times["after"] = float(sim.env.now)
            assert len(fr) == 5
            yield from s.send_status(fr[-1].item)
            times["status"] = float(sim.env.now)

        _run(sim, body)

        charged = times["after"] - times["before"]
        assert charged == pytest.approx(5 * SLOT), (
            "five items must cost five slots — an uncharged frame read is rate-blind, and "
            "rate-blind twins report zero loss where the hardware loses samples")
        assert charged > 0, "the negative control: reporting for free is the defect"
        assert times["status"] >= times["before"] + 5 * SLOT, (
            "the verdict cannot reach the producer before the items it is about have played")

    def test_the_frame_reader_needs_a_slot_period(self):
        """No default, because a default here would silently be the free-report defect."""
        sim, iface, m, s = _acked(max_in_flight=2, slot_period=None)

        def body():
            yield from m.write_frame(_w(1, 2), token="t")
            with pytest.raises(RuntimeError, match="needs slot_period"):
                yield from s.read_frame_nb()

        _run(sim, body)

    def test_the_two_readers_may_not_be_mixed(self):
        """One is the HLS twin and the other its LT approximation; a frame split across both would
        have two different notions of when it played."""
        sim, iface, m, s = _acked(max_in_flight=2)

        def body():
            yield from m.write_frame(_w(1, 2, 3), token="t")
            assert (yield from s.read_nb()) is not None
            with pytest.raises(RuntimeError, match="left over from read_nb"):
                yield from s.read_frame_nb()

        _run(sim, body)

    def test_the_frame_reader_is_non_blocking_when_empty(self):
        sim, iface, m, s = _acked(max_in_flight=2)

        def body():
            t0 = float(sim.env.now)
            assert (yield from s.read_frame_nb()) is None
            assert float(sim.env.now) == t0, "nothing to play, nothing to charge"

        _run(sim, body)


class TestWiringAndSizing:
    """The mistakes that would present as a hang rather than an error, refused at bind."""

    def test_the_reverse_channel_cannot_be_wired_backwards(self):
        """The credit channel's master is the data **slave**; the interface does that wiring so a
        caller cannot get it the wrong way round."""
        sim, iface, m, s = _credit()
        assert iface.crd_if.endpoints["master"] is s.crd_ep
        assert iface.crd_if.endpoints["slave"] is m.crd_ep
        assert iface.fwd_if.endpoints["master"] is m.fwd_ep
        assert iface.fwd_if.endpoints["slave"] is s.fwd_ep

    def test_the_forward_depth_is_the_one_the_producer_computes_credit_from(self):
        """``avail`` is meaningless if the queue it names is not the queue that fills."""
        sim, iface, m, s = _credit(depth=12)
        assert m.depth == 12
        assert s.fwd_ep.nrx.capacity == 12

    def test_mismatched_counter_widths_are_refused(self):
        sim = Simulation()
        m = CreditStreamMasterIF(name="m", sim=sim, bitwidth=32, ctr_bits=16)
        s = CreditStreamSlaveIF(name="s", sim=sim, bitwidth=32, ctr_bits=8)
        iface = CreditStreamIF(name="c", sim=sim, clk=Clock(freq=FREQ), bitwidth=32, depth=8)
        iface.bind("master", m)
        with pytest.raises(ValueError, match="counter widths differ"):
            iface.bind("slave", s)

    def test_an_unbounded_credit_channel_is_refused(self):
        sim = Simulation()
        with pytest.raises(ValueError, match="may not be None"):
            CreditStreamIF(name="c", sim=sim, clk=Clock(freq=FREQ), bitwidth=32, depth=None)

    def test_an_unbound_master_has_no_credit_to_report(self):
        sim = Simulation()
        m = CreditStreamMasterIF(name="m", sim=sim, bitwidth=32)
        with pytest.raises(RuntimeError, match="not bound"):
            _ = m.avail


class TestTheTypedReadPath:
    """``CreditStreamSlaveIF.get`` is a drop-in for a plain stream slave, typed reads included.

    The credit accounting must charge the words the *schema* occupied, not the number of schema
    instances — a difference that is invisible at one word per instance and wrong everywhere else.
    """

    def test_a_typed_read_credits_the_words_it_consumed(self):
        from waveflow.hw.dataschema import DataList, IntField

        class TwoWords(DataList):
            elements = {
                "a": {"schema": IntField.specialize(bitwidth=32, signed=False)},
                "b": {"schema": IntField.specialize(bitwidth=32, signed=False)},
            }

        assert TwoWords.nwords_per_inst(32) == 2, "the premise of this test"
        sim, iface, m, s = _credit(depth=8, credit_depth=4)

        def body():
            msg = TwoWords()
            msg.a, msg.b = 11, 22
            assert (yield from m.write_nb(msg)) is True
            assert m.written == 2, "two words, not one instance"
            assert m.avail == 5

            got = yield from s.get(TwoWords)
            assert (int(got.a), int(got.b)) == (11, 22)
            assert s.consumed == 2
            yield from m.poll_credit(1)
            assert m.acked == 2
            assert m.avail == 7

        _run(sim, body)
