"""
Unit tests for waveflow/hw/aximm_queue.py.

Coverage
--------
AXIMMQueueLayout  — address math across mem_bw and elem_words, validation
AXIMMQueue        — try_write/try_get core, blocking write/get, concurrent SPSC

The ring is backed by a real ``MemModel`` (decision 9): ``inline=False`` plus
a single ``alloc`` puts the backing segment at byte 0, which is exactly where
both the DirectMMIF (pass-through, base_addr=0) and crossbar (global − base)
paths deliver their local addresses.
"""
from __future__ import annotations

import numpy as np
import numpy.testing as npt
import pytest

import inspect

from waveflow.hw.aximm_queue import (
    AXIMMQueue,
    AXIMMQueueLayout,
    _split,
)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIFSlave
from waveflow.hw.memif import (
    AXIMMCrossBarIF,
    DirectMMIF,
    MMIFMaster,
    assign_address_ranges,
)
from waveflow.hw.memory import MemModel
from waveflow.simulation.simulation import Simulation


# A 2-field struct used by the typed-access tests; each field is one 32-bit
# word, so a Pair occupies elem_words == 2 at mem_bw == 32.
_U32 = IntField.specialize(bitwidth=32, signed=False)


class Pair(DataList):
    elements = {
        "a": {"schema": _U32, "description": "first word"},
        "b": {"schema": _U32, "description": "second word"},
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_mem(sim, layout, clk):
    """A ``MemModel`` backing *layout*'s ring region.

    ``inline=False`` + one ``alloc`` of the region's word count places the
    segment at byte 0; the slave then accepts the local addresses ``[0,
    total_bytes)`` that both interconnects deliver.
    """
    total_words = layout.total_bytes // layout.word_bytes
    mem = MemModel(sim=sim, word_size=layout.mem_bw, inline=False, clk=clk)
    mem.alloc(total_words)
    return mem


def _make_queue_direct(capacity, *, elem_words=1, mem_bw=32, base_addr=0x0):
    """One AXIMMQueue over a DirectMMIF + MemModel; returns (sim, queue)."""
    sim = Simulation()
    clk = Clock(freq=1.0)
    layout = AXIMMQueueLayout(
        base_addr=base_addr, capacity=capacity, elem_words=elem_words, mem_bw=mem_bw
    )
    mem = _make_mem(sim, layout, clk)
    master = MMIFMaster(sim=sim, bitwidth=mem_bw)
    direct = DirectMMIF(sim=sim, clk=clk)
    direct.bind("master", master)
    direct.bind("slave", mem.s_mm)
    return sim, AXIMMQueue(master=master, layout=layout)


def _make_spsc_crossbar(sim, layout, clk, *, latency_init=0.0, latency_read_return=0.0):
    """Producer/consumer AXIMMQueues over a 2-master crossbar + MemModel.

    Returns ``(producer_queue, consumer_queue)`` sharing one ring region.
    """
    mem = _make_mem(sim, layout, clk)
    xbar = AXIMMCrossBarIF(
        sim=sim, clk=clk,
        nports_master=2, nports_slave=1, bitwidth=layout.mem_bw,
        latency_init=latency_init, latency_read_return=latency_read_return,
    )
    prod_master = MMIFMaster(sim=sim, bitwidth=layout.mem_bw)
    cons_master = MMIFMaster(sim=sim, bitwidth=layout.mem_bw)
    xbar.bind("master_0", prod_master)
    xbar.bind("master_1", cons_master)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(layout.base_addr, layout.total_bytes)])
    return (
        AXIMMQueue(master=prod_master, layout=layout),
        AXIMMQueue(master=cons_master, layout=layout),
    )


# ---------------------------------------------------------------------------
# AXIMMQueueLayout
# ---------------------------------------------------------------------------

class TestAXIMMQueueLayout:
    def test_basic_math_32bit(self):
        lay = AXIMMQueueLayout(base_addr=0x1000, capacity=8, elem_words=1, mem_bw=32)
        assert lay.word_bytes == 4
        assert lay.control_bytes == 16          # 4 control words * 4 bytes
        assert lay.head_addr == 0x1000
        assert lay.tail_addr == 0x1004
        assert lay.capacity_addr == 0x1008
        assert lay.data_base == 0x1010
        assert lay.slot_addr(0) == 0x1010
        assert lay.slot_addr(7) == 0x1010 + 7 * 4
        # 16 control bytes + 8 slots * 1 word * 4 bytes
        assert lay.total_bytes == 16 + 8 * 4

    def test_mem_bw_64_doubles_control_and_stride(self):
        """Regression: a hard-coded 0x04 / CONTROL_BYTES=16 would fail this."""
        lay32 = AXIMMQueueLayout(base_addr=0, capacity=8, mem_bw=32)
        lay64 = AXIMMQueueLayout(base_addr=0, capacity=8, mem_bw=64)
        assert lay64.word_bytes == 8
        assert lay64.control_bytes == 32        # double the 32-bit case
        assert lay64.control_bytes == 2 * lay32.control_bytes
        # tail is one word past head — stride doubles with mem_bw
        assert lay32.tail_addr == 4
        assert lay64.tail_addr == 8
        # data base sits after the (larger) control region
        assert lay64.data_base == 32
        assert lay64.slot_addr(1) - lay64.slot_addr(0) == 8

    def test_elem_words_scales_slot_stride(self):
        lay = AXIMMQueueLayout(base_addr=0, capacity=4, elem_words=4, mem_bw=32)
        assert lay.slot_addr(0) == lay.data_base
        # each slot is 4 words * 4 bytes = 16 bytes
        assert lay.slot_addr(1) - lay.slot_addr(0) == 16
        assert lay.slot_addr(3) == lay.data_base + 3 * 16
        assert lay.total_bytes == lay.control_bytes + 4 * 4 * 4

    def test_elem_words_4_mem_bw_64(self):
        lay = AXIMMQueueLayout(base_addr=0x40, capacity=4, elem_words=4, mem_bw=64)
        assert lay.word_bytes == 8
        assert lay.control_bytes == 32
        assert lay.data_base == 0x40 + 32
        # slot stride = elem_words(4) * word_bytes(8) = 32
        assert lay.slot_addr(1) - lay.slot_addr(0) == 32
        assert lay.total_bytes == 32 + 4 * 4 * 8

    def test_validation(self):
        with pytest.raises(ValueError, match="capacity"):
            AXIMMQueueLayout(base_addr=0, capacity=1)
        with pytest.raises(ValueError, match="elem_words"):
            AXIMMQueueLayout(base_addr=0, capacity=4, elem_words=0)
        with pytest.raises(ValueError, match="mem_bw"):
            AXIMMQueueLayout(base_addr=0, capacity=4, mem_bw=128)


# ---------------------------------------------------------------------------
# _split wrap helper
# ---------------------------------------------------------------------------

class TestSplit:
    def test_no_wrap(self):
        assert _split(2, 3, 8) == [(2, 3)]

    def test_exact_end(self):
        assert _split(5, 3, 8) == [(5, 3)]

    def test_wrap(self):
        assert _split(6, 5, 8) == [(6, 2), (0, 3)]

    def test_full_wrap_from_zero(self):
        assert _split(0, 8, 8) == [(0, 8)]


# ---------------------------------------------------------------------------
# AXIMMQueue core (try_write / try_get), elem_words == 1
# ---------------------------------------------------------------------------

class TestAXIMMQueueCore:
    def test_mem_bw_mismatch_raises(self):
        sim = Simulation()
        master = MMIFMaster(sim=sim, bitwidth=32)
        layout = AXIMMQueueLayout(base_addr=0, capacity=8, mem_bw=64)
        with pytest.raises(ValueError, match="mem_bw"):
            AXIMMQueue(master=master, layout=layout)

    def test_fifo_order_many_cycles(self):
        sim, q = _make_queue_direct(capacity=8)
        out = []

        def proc():
            yield from q.reset()
            nxt = 0
            for _ in range(20):
                batch = np.arange(nxt, nxt + 3, dtype=np.uint32)
                ok = yield from q.try_write(batch)
                assert ok
                nxt += 3
                got = yield from q.try_get(3)
                out.append(np.array(got))

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(np.concatenate(out), np.arange(60))

    def test_full_returns_false(self):
        sim, q = _make_queue_direct(capacity=8)   # usable depth 7
        result = {}

        def proc():
            yield from q.reset()
            ok = yield from q.try_write(np.arange(7, dtype=np.uint32))
            result["fill"] = ok
            result["space"] = yield from q.space()
            result["count"] = yield from q.count()
            # one more slot must not fit
            result["overflow"] = yield from q.try_write(np.array([99], dtype=np.uint32))

        sim.env.process(proc())
        sim.env.run()
        assert result["fill"] is True
        assert result["space"] == 0
        assert result["count"] == 7
        assert result["overflow"] is False

    def test_drain_to_empty(self):
        sim, q = _make_queue_direct(capacity=8)
        result = {}

        def proc():
            yield from q.reset()
            yield from q.try_write(np.arange(5, dtype=np.uint32))
            got = yield from q.try_get(5)
            result["drained"] = np.array(got)
            result["count"] = yield from q.count()
            # empty now: try_get returns empty
            empty = yield from q.try_get(3)
            result["empty_len"] = len(empty)

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(result["drained"], np.arange(5))
        assert result["count"] == 0
        assert result["empty_len"] == 0

    def test_wrap_around_integrity(self):
        """capacity 8, repeatedly write 5 / get 5 forces head/tail past the end."""
        sim, q = _make_queue_direct(capacity=8)
        out = []

        def proc():
            yield from q.reset()
            nxt = 0
            for _ in range(10):
                ok = yield from q.try_write(np.arange(nxt, nxt + 5, dtype=np.uint32))
                assert ok
                got = yield from q.try_get(5)
                out.append(np.array(got))
                nxt += 5

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(np.concatenate(out), np.arange(50))

    def test_partial_get_short(self):
        """try_get with max_slots > available returns only what's there."""
        sim, q = _make_queue_direct(capacity=8)
        result = {}

        def proc():
            yield from q.reset()
            yield from q.try_write(np.array([10, 11], dtype=np.uint32))
            got = yield from q.try_get(5)   # only 2 available
            result["got"] = np.array(got)

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(result["got"], [10, 11])

    def test_space_count_track(self):
        sim, q = _make_queue_direct(capacity=8)
        trace = []

        def proc():
            yield from q.reset()
            for n in (1, 2, 3):
                yield from q.try_write(np.arange(n, dtype=np.uint32))
                c = yield from q.count()
                s = yield from q.space()
                trace.append((c, s))

        sim.env.process(proc())
        sim.env.run()
        # cumulative counts 1, 3, 6; space = 7 - count
        assert trace == [(1, 6), (3, 4), (6, 1)]

    def test_wrap_split_in_single_write(self):
        """A batch that itself straddles the wrap is stored correctly."""
        sim, q = _make_queue_direct(capacity=8)
        result = {}

        def proc():
            yield from q.reset()
            # advance tail to 6: write 6, get 6
            yield from q.try_write(np.arange(6, dtype=np.uint32))
            yield from q.try_get(6)
            # now head=tail=6; write 4 slots -> wraps [6,8)+[0,2)
            yield from q.try_write(np.array([100, 101, 102, 103], dtype=np.uint32))
            got = yield from q.try_get(4)
            result["got"] = np.array(got)

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(result["got"], [100, 101, 102, 103])


# ---------------------------------------------------------------------------
# Blocking write/get + concurrent SPSC over a crossbar
# ---------------------------------------------------------------------------

class TestBlocking:
    def test_blocking_write_get_single_process(self):
        sim, q = _make_queue_direct(capacity=4)   # usable depth 3
        result = {}

        def proc():
            yield from q.reset()
            yield from q.write(np.array([1, 2, 3], dtype=np.uint32))
            got = yield from q.get(count=3)
            result["got"] = np.array(got)

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(result["got"], [1, 2, 3])

    def test_get_zero_returns_empty(self):
        """get(0) returns an empty array without indexing an empty list."""
        sim, q = _make_queue_direct(capacity=4)
        result = {}

        def proc():
            yield from q.reset()
            got = yield from q.get(count=0)
            result["got"] = np.array(got)

        sim.env.process(proc())
        sim.env.run()
        assert len(result["got"]) == 0


class TestConcurrentSPSC:
    def test_producer_consumer_crossbar(self):
        """Two masters, one shared region: producer blocks on full, consumer
        drains; data must arrive in order with no loss or duplication."""
        sim = Simulation()
        clk = Clock(freq=1.0)
        N = 100
        layout = AXIMMQueueLayout(base_addr=0x1000, capacity=8, mem_bw=32)
        pq, cq = _make_spsc_crossbar(
            sim, layout, clk, latency_init=1.0, latency_read_return=1.0
        )

        data = np.arange(N, dtype=np.uint32)
        result = {}

        def producer():
            yield from pq.reset()
            for i in range(0, N, 4):
                yield from pq.write(data[i:i + 4], poll_interval=1.0)

        def consumer():
            got = yield from cq.get(count=N, poll_interval=1.0)
            result["got"] = np.array(got)

        sim.env.process(producer())
        sim.env.process(consumer())
        sim.env.run()

        npt.assert_array_equal(result["got"], data)

    def test_consumer_starts_before_producer(self):
        """Consumer that begins polling on an empty queue still receives all
        data once the producer runs."""
        sim = Simulation()
        clk = Clock(freq=1.0)
        N = 30
        layout = AXIMMQueueLayout(base_addr=0x0, capacity=6, mem_bw=32)
        pq, cq = _make_spsc_crossbar(sim, layout, clk)
        data = np.arange(N, dtype=np.uint32) + 1000
        result = {}

        def producer():
            yield from pq.reset()
            yield pq.master.timeout(5.0)   # let the consumer poll an empty queue first
            for i in range(0, N, 5):
                yield from pq.write(data[i:i + 5], poll_interval=0.5)

        def consumer():
            got = yield from cq.get(count=N, poll_interval=0.5)
            result["got"] = np.array(got)

        sim.env.process(producer())
        sim.env.process(consumer())
        sim.env.run()
        npt.assert_array_equal(result["got"], data)

    def test_write_larger_than_depth_streams(self):
        """A single ``write`` of an array far larger than the usable depth
        streams through, blocking as the consumer drains — the case the old
        all-or-nothing ``write`` raised on (decision 8)."""
        sim = Simulation()
        clk = Clock(freq=1.0)
        N = 1000
        layout = AXIMMQueueLayout(base_addr=0x0, capacity=8, mem_bw=32)  # usable 7
        pq, cq = _make_spsc_crossbar(sim, layout, clk)
        data = np.arange(N, dtype=np.uint32)
        result = {}

        def producer():
            yield from pq.reset()
            # One write call for the whole 1000-word array (>> usable depth 7).
            yield from pq.write(data, poll_interval=1.0)

        def consumer():
            got = yield from cq.get(count=N, poll_interval=1.0)
            result["got"] = np.array(got)

        sim.env.process(producer())
        sim.env.process(consumer())
        sim.env.run()
        npt.assert_array_equal(result["got"], data)


# ---------------------------------------------------------------------------
# Multi-word slots (elem_words > 1), raw words
# ---------------------------------------------------------------------------

class TestMultiWordSlots:
    def test_elem_words_4_wrap_and_fifo(self):
        """A 4-word-per-slot ring preserves FIFO and integrity across the wrap.

        capacity 4 (usable 3); repeatedly write 3 slots / get 3 slots forces the
        slot pointers past the end so the wrap split (decision 10) is exercised
        with multi-word slots.
        """
        sim, q = _make_queue_direct(capacity=4, elem_words=4)
        out = []

        def proc():
            yield from q.reset()
            nxt = 0
            for _ in range(8):
                words = np.arange(nxt, nxt + 3 * 4, dtype=np.uint32)  # 3 slots
                ok = yield from q.try_write(words)
                assert ok
                got = yield from q.try_get(3)
                out.append(np.array(got))
                nxt += 3 * 4

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(np.concatenate(out), np.arange(8 * 3 * 4))

    def test_blocking_get_raw_by_nwords_max(self):
        """The raw path also accepts nwords_max (a word cap), like the stream."""
        sim, q = _make_queue_direct(capacity=8, elem_words=2)
        result = {}

        def proc():
            yield from q.reset()
            yield from q.write(np.arange(6, dtype=np.uint32))  # 3 slots
            got = yield from q.get(nwords_max=6)               # 6 words = 3 slots
            result["got"] = np.array(got)

        sim.env.process(proc())
        sim.env.run()
        npt.assert_array_equal(result["got"], np.arange(6))


# ---------------------------------------------------------------------------
# Typed write/get (stream signature, decision 6)
# ---------------------------------------------------------------------------

class TestTyped:
    def test_typed_roundtrip_count(self):
        """write(element_type=Pair) then get(Pair, count=N) round-trips fields."""
        sim, q = _make_queue_direct(capacity=8, elem_words=2)   # Pair = 2 words
        pairs = [Pair(a=i, b=i + 100) for i in range(5)]
        result = {}

        def proc():
            yield from q.reset()
            yield from q.write(pairs, Pair)
            got = yield from q.get(Pair, count=5)
            # The count path returns a DataArray; .val is a list of dict rows.
            result["pairs"] = [(int(r["a"]), int(r["b"])) for r in got.val]

        sim.env.process(proc())
        sim.env.run()
        assert result["pairs"] == [(i, i + 100) for i in range(5)]

    def test_typed_single_instance(self):
        """get(Pair) with no count returns one deserialized Pair instance."""
        sim, q = _make_queue_direct(capacity=8, elem_words=2)
        result = {}

        def proc():
            yield from q.reset()
            yield from q.write([Pair(a=7, b=9)], Pair)
            got = yield from q.get(Pair)
            result["pair"] = (int(got.a), int(got.b))

        sim.env.process(proc())
        sim.env.run()
        assert result["pair"] == (7, 9)

    def test_typed_wrap_around(self):
        """Typed access survives the ring wrap (multi-word slots)."""
        sim, q = _make_queue_direct(capacity=4, elem_words=2)   # usable 3 slots
        out = []

        def proc():
            yield from q.reset()
            nxt = 0
            for _ in range(6):
                batch = [Pair(a=nxt + j, b=nxt + j + 1000) for j in range(3)]
                yield from q.write(batch, Pair)
                got = yield from q.get(Pair, count=3)
                out.extend((int(r["a"]), int(r["b"])) for r in got.val)
                nxt += 3

        sim.env.process(proc())
        sim.env.run()
        expected = [(n, n + 1000) for n in range(6 * 3)]
        assert out == expected

    def test_get_signature_matches_stream(self):
        """The dequeue signature reuses StreamIFSlave.get exactly (decision 6)."""
        q_params = inspect.signature(AXIMMQueue.get).parameters
        s_params = inspect.signature(StreamIFSlave.get).parameters
        # Every stream parameter (schema_type, count, nwords_max) is present with
        # the same kind on the queue; the queue only adds poll_interval.
        for name in ("schema_type", "count", "nwords_max"):
            assert name in q_params, f"queue get is missing {name}"
            assert q_params[name].kind == s_params[name].kind
        assert set(s_params) - {"self"} <= set(q_params)
        assert "poll_interval" in q_params

    def test_typed_elem_words_mismatch_raises(self):
        """Typed access against a layout whose elem_words disagrees with the
        schema raises a clear error (decision 11, typed layer)."""
        sim, q = _make_queue_direct(capacity=8, elem_words=1)   # Pair needs 2

        def write_proc():
            yield from q.reset()
            yield from q.write([Pair(a=1, b=2)], Pair)

        sim.env.process(write_proc())
        with pytest.raises(ValueError, match="elem_words"):
            sim.env.run()

        sim2, q2 = _make_queue_direct(capacity=8, elem_words=1)

        def get_proc():
            yield from q2.reset()
            yield from q2.get(Pair, count=1)

        sim2.env.process(get_proc())
        with pytest.raises(ValueError, match="elem_words"):
            sim2.env.run()
