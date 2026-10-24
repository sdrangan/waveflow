"""tests/hw/test_stream_begin_end.py — a stream transfer as two events (``plans/pipelined_ops.md`` Stage 2).

A transfer has a **first** and a **last** beat.  The stream primitive now keeps both, as stamps on
every burst, and exposes the transfer's two halves (``begin`` / ``end``) so a pipelined body can act
between them.  ``write`` and ``get`` are ``begin`` then ``end`` at once, and must do exactly what they
did before: same waits, same events, same times.

The rules these tests hold (the plan's Stage 1 record):

* every burst carries ``(first, nominal, delivered)``: its first word's arrival
  ``start + latency_init + 1`` cycles, its nominal last ``start + latency_init + n``, and when admission
  let it in;
* a begun burst holds **no** room -- admission stays at the writer's end;
* a reader may take a begun burst (``pending_ok``) and has its words at once; its ``end`` waits for the
  delivery and retires the words;
* ``Xfer.last`` raises while the transfer is in flight.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simulation import Simulation


def _chan(depth=4, latency_init=0, has_tlast=True):
    """One stream at 1 Hz, so simulated times are cycles."""
    sim = Simulation()
    iface = StreamIF(name="ch", sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=depth,
                     latency_init=latency_init)
    m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=has_tlast)
    s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=has_tlast)
    iface.bind("master", m)
    iface.bind("slave", s)
    return sim.env, m, s


def _words(n):
    return np.arange(n, dtype=np.uint64)


# ---------------------------------------------------------------------------------------------------
# write / get are unchanged
# ---------------------------------------------------------------------------------------------------

def _reference_scenario():
    """Six bursts, one larger than the queue, against a consumer that alternates slow and fast,
    with wire latency -- the times and the event count recorded on the code before Stage 2."""
    env, m, s = _chan(depth=4, latency_init=2)
    wrote, got = [], []

    def producer():
        for n in (3, 4, 6, 2, 1, 5):
            yield from m.write(_words(n))
            wrote.append(env.now)

    def consumer():
        for k in range(6):
            yield env.timeout(7 if k % 2 else 1)
            w = yield from s.get()
            got.append((env.now, len(w)))

    env.process(producer())
    env.process(consumer())
    env.run()
    return next(env._eid), wrote, got, s


class TestWriteAndGetUnchanged:
    def test_same_times_and_events_as_before(self):
        """Recorded on the code before Stage 2 (``plans/witness/pipelined_ops``): 18 SimPy events.
        The split must add none and move nothing."""
        events, wrote, got, _ = _reference_scenario()
        assert wrote == [5.0, 11.0, 19.0, 23.0, 26.0, 33.0]
        assert got == [(5.0, 3), (12.0, 4), (19.0, 6), (26.0, 2), (27.0, 1), (34.0, 5)]
        assert events == 18

    def test_every_stamp_is_consumed(self):
        """The stamp FIFO stays in step with the buffer: one stamp a burst, all taken."""
        *_, s = _reference_scenario()
        assert len(s.burst_stamps) == 0 and len(s.data_buffer.items) == 0
        assert s.pending_burst is None


# ---------------------------------------------------------------------------------------------------
# stamps
# ---------------------------------------------------------------------------------------------------

class TestStamps:
    def test_first_nominal_delivered(self):
        """latency 2, 4 words, started at 0: first word at 3, nominal last at 6, delivered at 6."""
        env, m, s = _chan(depth=8, latency_init=2)
        seen = {}

        def reader():
            x = yield from s.begin_read()
            seen["first"] = x.first
            yield from s.end_read(x)
            seen["last"] = x.last
            seen["stamp"] = x.stamp

        env.process(m.write(_words(4)))
        env.process(reader())
        env.run()
        assert seen["stamp"] == (3.0, 6.0, 6.0)
        assert seen["first"] == 3.0 and seen["last"] == 6.0

    def test_the_stamp_is_the_old_back_calculation_when_nothing_stalls(self):
        """``get_pipelined`` back-calculates ``now - (n - 1)``; on an unstalled read that waited for
        the burst, the stamp gives the same first word -- the 'agree' column of the Stage 1 table."""
        env, m, s = _chan(depth=8)
        seen = {}

        def reader():
            x = yield from s.begin_read()
            yield from s.end_read(x)
            seen["back_calc"] = env.now - (x.nwords - 1)
            seen["first"] = x.first

        env.process(m.write(_words(5)))
        env.process(reader())
        env.run()
        assert seen["first"] == seen["back_calc"] == 1.0

    def test_a_buffered_burst_is_first_read_when_asked(self):
        """Delivered at 4, asked at 20: its first word is read at 20, not at 1 (where the stamp says
        it arrived) nor at 17 (the old back-calculation) -- the middle column of the Stage 1 table."""
        env, m, s = _chan(depth=8)
        seen = {}

        def reader():
            yield env.timeout(20)
            x = yield from s.begin_read()
            yield from s.end_read(x)
            seen["x"] = x

        env.process(m.write(_words(4)))
        env.process(reader())
        env.run()
        x = seen["x"]
        assert x.stamp.first == 1.0 and x.first == 20.0 and x.last == 20.0

    def test_back_pressure_marks_the_writer_stalled(self):
        """Queue of 2: the second 2-word burst waits for room until the reader takes the first."""
        env, m, s = _chan(depth=2)
        xs = []

        def writer():
            for _ in range(2):
                x = yield from m.begin(_words(2))
                yield from m.end(x)
                xs.append(x)

        def reader():
            yield env.timeout(10)
            for _ in range(2):
                yield from s.get()

        env.process(writer())
        env.process(reader())
        env.run()
        assert not xs[0].stalled and xs[0].last == 2.0
        assert xs[1].stalled and xs[1].last == 10.0      # nominal 4, let in when room appeared

    def test_offer_bursts_are_stamped_too(self):
        """Both producers stamp, so the FIFO cannot fall out of step when a stream mixes them."""
        env, m, s = _chan(depth=8, latency_init=1)
        stamps = []

        def producer():
            yield from m.offer(_words(3))
            yield from m.write(_words(2))

        def reader():
            for _ in range(2):
                x = yield from s.begin_read()
                yield from s.end_read(x)
                stamps.append(x.stamp)

        env.process(producer())
        env.process(reader())
        env.run()
        assert stamps[0] == (2.0, 4.0, 4.0)
        assert stamps[1] == (6.0, 7.0, 7.0)


# ---------------------------------------------------------------------------------------------------
# begin / end
# ---------------------------------------------------------------------------------------------------

class TestBeginEnd:
    def test_a_pending_burst_is_read_at_its_first_word(self):
        """8 words: the reader taking a begun burst has its words at the first word (1) and ends at
        the delivery (8)."""
        env, m, s = _chan(depth=16)
        seen = {}

        def reader():
            x = yield from s.begin_read(pending_ok=True)
            seen["begin"] = env.now
            seen["data"] = list(x.data)
            yield from s.end_read(x)
            seen["end"] = env.now

        env.process(m.write(_words(8)))
        env.process(reader())
        env.run()
        assert seen["begin"] == 1.0 and seen["end"] == 8.0
        assert seen["data"] == list(range(8))

    def test_a_begun_burst_holds_no_room(self):
        """Between the writer's begin and end the queue's room is untouched: admission is at end."""
        env, m, s = _chan(depth=4)
        levels = {}

        def writer():
            x = yield from m.begin(_words(3))
            levels["begun"] = s.nrx.level
            yield env.timeout(1)
            levels["later"] = s.nrx.level
            yield from m.end(x)
            levels["ended"] = s.nrx.level

        env.process(writer())
        env.run()
        assert levels == {"begun": 0, "later": 0, "ended": 3}

    def test_the_pr185_case_does_not_deadlock(self):
        """PR #185's deadlock: a burst larger than its queue.  Three 3-word bursts into a 2-deep
        queue, read by a pipelined reader that takes each burst begun -- all complete, in order."""
        env, m, s = _chan(depth=2)
        got = []

        def writer():
            for k in range(3):
                yield from m.write(_words(3) + 10 * k)

        def reader():
            for _ in range(3):
                x = yield from s.begin_read(pending_ok=True)
                yield from s.end_read(x)
                got.append(int(x.data[0]))

        env.process(writer())
        env.process(reader())
        env.run(until=1000)
        assert got == [0, 10, 20]

    def test_writer_markers(self):
        """The writer's first is its first beat, a cycle after the transfer starts; its last the
        delivery."""
        env, m, s = _chan(depth=8, latency_init=1)
        seen = {}

        def writer():
            yield env.timeout(5)
            x = yield from m.begin(_words(4))
            seen["first"] = x.first
            yield from m.end(x)
            seen["last"] = x.last

        env.process(writer())
        env.run()
        assert seen == {"first": 6.0, "last": 10.0}

    def test_last_raises_while_in_flight(self):
        env, m, s = _chan(depth=8)
        seen = {}

        def writer():
            x = yield from m.begin(_words(2))
            with pytest.raises(RuntimeError, match="in flight"):
                _ = x.last
            seen["ok"] = True
            yield from m.end(x)

        env.process(writer())
        env.run()
        assert seen["ok"]

    def test_index_counts_transfers_per_endpoint(self):
        env, m, s = _chan(depth=8)
        idx = []

        def reader():
            for _ in range(3):
                x = yield from s.begin_read()
                yield from s.end_read(x)
                idx.append(x.index)

        def writer():
            for _ in range(3):
                yield from m.write(_words(1))

        env.process(writer())
        env.process(reader())
        env.run()
        assert idx == [0, 1, 2]


class TestOneBeatACycle:
    def test_the_next_transfer_starts_a_cycle_after_the_last_beat(self):
        """A port takes one beat a cycle: two bursts waiting in the buffer, read back to back -- the
        first is read 20 .. 23 (a word a cycle), the second's first beat is 24, not 23."""
        env, m, s = _chan(depth=16)
        seen = []

        def writer():
            for k in range(2):
                yield from m.write(_words(4) + 10 * k)

        def reader():
            yield env.timeout(20)
            for _ in range(2):
                x = yield from s.begin_read()
                yield from s.end_read(x, charge_beats=True)
                seen.append((x.first, x.last))

        env.process(writer())
        env.process(reader())
        env.run()
        assert seen == [(20.0, 23.0), (24.0, 27.0)]
