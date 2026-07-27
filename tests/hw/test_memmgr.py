"""Tests for :class:`~waveflow.hw.memory.MemMgr` — the allocator/addressing policy.

The point of the class is that it owns **no bytes**: address conversion and placement can be
exercised, and reasoned about, without a backing store anywhere in sight.
"""
from __future__ import annotations

import pytest

from waveflow.hw.memory import AddrUnit, MemMgr, Memory


def test_byte_addressing_round_trips():
    m = MemMgr(word_size=32, addr_unit=AddrUnit.byte)
    assert m.index_to_addr(4) == 16
    assert m.addr_to_index(16) == 4


def test_word_addressing_is_the_identity():
    m = MemMgr(word_size=32, addr_unit=AddrUnit.word)
    assert m.index_to_addr(7) == 7
    assert m.addr_to_index(7) == 7


def test_misaligned_byte_address_is_rejected():
    m = MemMgr(word_size=64, addr_unit=AddrUnit.byte)
    with pytest.raises(ValueError, match="not aligned"):
        m.addr_to_index(4)          # 4 bytes into an 8-byte word


def test_non_byte_word_size_is_rejected_for_byte_addressing():
    m = MemMgr(word_size=12, addr_unit=AddrUnit.byte)
    with pytest.raises(ValueError, match="multiple of 8"):
        m.addr_to_index(0)


def test_place_is_first_fit():
    """A gap big enough wins over appending at the end — that IS first-fit."""
    m = MemMgr(word_size=32, nwords_tot=100)
    assert m.place(4, [(0, 8), (16, 32)]) == 8      # the 8..16 gap fits
    assert m.place(16, [(0, 8), (16, 32)]) == 32    # too big for the gap; goes after


def test_place_starts_at_zero_when_nothing_is_occupied():
    assert MemMgr(word_size=32).place(10, []) == 0


def test_place_refuses_to_exceed_capacity():
    m = MemMgr(word_size=32, nwords_tot=16)
    with pytest.raises(MemoryError, match="Unable to allocate"):
        m.place(8, [(0, 12)])


def test_place_is_unbounded_when_capacity_is_none():
    m = MemMgr(word_size=32, nwords_tot=None)
    assert m.place(1_000_000, [(0, 4)]) == 4


def test_place_rejects_a_non_positive_size():
    with pytest.raises(ValueError, match="positive"):
        MemMgr(word_size=32).place(0, [])


def test_memory_delegates_to_its_manager():
    """Memory keeps the bytes and asks the manager where they go — one implementation, not two."""
    mem = Memory(word_size=32, nwords_tot=64)
    assert isinstance(mem.mgr, MemMgr)
    a = mem.alloc(4)
    b = mem.alloc(4)
    # Byte-addressed 32-bit words: the second allocation lands 4 words (16 bytes) along.
    assert (a, b) == (0, 16)
    assert mem.mgr.addr_to_index(b) == 4


def test_freeing_reopens_the_gap_for_first_fit():
    """The manager is handed the live occupancy, so a free is visible to the next placement."""
    mem = Memory(word_size=32, nwords_tot=64)
    a = mem.alloc(4)
    mem.alloc(4)
    mem.free(a)
    assert mem.alloc(4) == a
