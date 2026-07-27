"""memmgr_demo.py — :class:`MemMgr`, the allocator that owns no bytes.

Backs ``docs/guide/memory/memmgr.md``.  The whole point of the class is visible here: every
question it answers — where does this fit, what word index is this address — is answered without a
backing store anywhere in the file.  Run it directly, or let
``tests/examples/test_memory_demos.py`` run it.
"""
from __future__ import annotations

from waveflow.hw.memory import AddrUnit, MemMgr, Memory


def addressing() -> None:
    """Byte vs word units — the only place the convention is implemented."""
    byte_mgr = MemMgr(word_size=32, addr_unit=AddrUnit.byte)
    word_mgr = MemMgr(word_size=32, addr_unit=AddrUnit.word)

    # Four 32-bit words in: 16 bytes, or word index 4, depending on what an address MEANS.
    assert byte_mgr.index_to_addr(4) == 16
    assert word_mgr.index_to_addr(4) == 4
    assert byte_mgr.addr_to_index(16) == 4

    # Misalignment is an error, not a rounding.  Silently truncating here would produce a
    # plausible-looking address that reads the wrong word.
    try:
        MemMgr(word_size=64, addr_unit=AddrUnit.byte).addr_to_index(4)
    except ValueError as exc:
        assert "not aligned" in str(exc)
    else:                                            # pragma: no cover - the point of the demo
        raise AssertionError("expected a misaligned byte address to raise")

    print("addressing: 4 words = 16 bytes (byte unit) / index 4 (word unit); misalignment raises")


def placement() -> None:
    """First-fit over ranges the manager is HANDED — it tracks nothing itself."""
    mgr = MemMgr(word_size=32, nwords_tot=64)

    # Occupied: [0,8) and [16,32).  A 4-word request fits the 8..16 gap; a 16-word one does not.
    assert mgr.place(4, [(0, 8), (16, 32)]) == 8
    assert mgr.place(16, [(0, 8), (16, 32)]) == 32

    # Capacity is enforced, loudly.
    try:
        MemMgr(word_size=32, nwords_tot=16).place(8, [(0, 12)])
    except MemoryError as exc:
        assert "Unable to allocate" in str(exc)
    else:                                            # pragma: no cover - the point of the demo
        raise AssertionError("expected an over-capacity placement to raise")

    print("placement: first-fit uses the 8..16 gap for 4 words, appends at 32 for 16")


def with_a_store() -> None:
    """How ``Memory`` uses it: the manager decides WHERE, the store holds WHAT.

    The visible consequence of handing the manager live occupancy is that a free reopens its gap
    to the very next placement — there is no stale allocation table to go out of date.
    """
    mem = Memory(word_size=32, nwords_tot=64)
    a = mem.alloc(4)
    b = mem.alloc(4)
    assert (a, b) == (0, 16)                         # byte addresses, 4 words apart
    assert mem.mgr.addr_to_index(b) == 4

    mem.free(a)
    assert mem.alloc(4) == a                         # the gap is immediately reusable

    print(f"with a store: alloc -> {a}, {b} (bytes); after free, the next alloc reuses {a}")


def main() -> None:
    addressing()
    placement()
    with_a_store()


if __name__ == "__main__":
    main()
