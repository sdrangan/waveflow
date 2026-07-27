---
title: MemMgr — allocation and addressing
parent: Memory Modeling
nav_order: 2
summary: "MemMgr is the allocator and the address arithmetic, and it owns no bytes at all — it models what an OS, a linker script, or a hand-rolled arena does. It converts between caller addresses and word indices (byte or word units, with alignment checking) and runs first-fit placement over the occupied ranges it is handed. It is handed those ranges rather than tracking them, so it can never disagree with the storage."
---

# `MemMgr` — allocation and addressing

`MemMgr` answers two questions and stores nothing:

- **Where does this go?** — first-fit placement (`place`).
- **What word index is this address?** — the addressing convention (`addr_to_index` /
  `index_to_addr`).

That is the whole class. It models the *manager* — an OS allocator, a linker script, a hand-rolled
arena — as distinct from the memory being managed.

```python
from waveflow.hw.memory import AddrUnit, MemMgr

mgr = MemMgr(word_size=32, nwords_tot=1024, addr_unit=AddrUnit.byte)
mgr.index_to_addr(4)      # 16  — four 32-bit words in
mgr.addr_to_index(16)     # 4
mgr.place(4, [(0, 8), (16, 32)])    # 8 — first-fit lands in the gap
```

## Addressing: the byte-vs-word convention

`AddrUnit` selects what an address *means*, and this is the only place the convention is
implemented:

| `addr_unit` | an address is… | typical use |
|---|---|---|
| `AddrUnit.byte` | a byte offset | AXI4 / DDR / PYNQ interfaces |
| `AddrUnit.word` | a word index | local arrays, BRAM-like interfaces |

Byte addressing divides by the word's byte width and **enforces alignment** — a misaligned address
raises rather than silently truncating:

```python
MemMgr(word_size=64, addr_unit=AddrUnit.byte).addr_to_index(4)
# ValueError: Address 4 is not aligned to the word size of 8 bytes.
```

Word addressing is the identity. It models hardware that indexes words directly, which is what an
HLS local array does.

A word size that is not a whole number of bytes is rejected for byte addressing, because the
conversion is not defined — again, loudly rather than by rounding.

## Placement: first-fit over ranges you supply

```python
def place(self, nwords: int, occupied: list[tuple[int, int]]) -> int
```

`occupied` is a sorted list of half-open `(start_index, end_index)` ranges. `place` walks them and
returns the first index where `nwords` fits, or raises `MemoryError` if nothing fits inside
`nwords_tot`.

The signature is the design decision worth explaining: **the manager is handed the occupancy rather
than tracking it.** A manager with its own allocation table would be a second source of truth about
what is occupied, and the two could drift — a freed segment the manager still believes is live, or
vice versa. Handing it the live ranges makes disagreement structurally impossible, at the cost of one
argument.

The consequence is visible in behaviour: freeing a segment immediately reopens its gap to the next
placement, because the next call sees the new occupancy.

```python
mem = Memory(word_size=32, nwords_tot=64)
a = mem.alloc(4); mem.alloc(4)
mem.free(a)
mem.alloc(4) == a          # True — first-fit found the reopened gap
```

## How `Memory` uses it

`Memory` keeps the bytes and delegates the policy. Its `alloc` is now two clearly separate steps —
*where* (the manager) and *what* (the storage):

```python
next_free = self.mgr.place(nwords, self._segment_bounds())
addr = self.mgr.index_to_addr(next_free)
self.segments[addr] = np.zeros(...)
```

Every existing `alloc` / `free` / `read` / `write` call site is unaffected; what changed is that the
policy now has a name.

## The C++ side

The name is shared on purpose. The generated Vitis testbench uses `MemMgr<word_dwidth>` from
`memmgr_tb.hpp`, and kernels use `memmgr::byte_addr_to_word_index<mem_dwidth>` for the same byte→word
conversion. Because both sides implement the same first-fit rule, addresses computed in Python are
valid in the C++ testbench — which is what lets a Python model and an HLS kernel agree on a layout
without either restating it. See the [Vitis page](./vitis.md).

## What it is not

- **Not storage.** It has no `segments`, no arrays, no bytes. If you want to hold data, you want
  [`Memory`](./python.md) or [`MemoryMod`](./memorymod.md).
- **Not timed.** Allocation is elaboration-time bookkeeping, not a simulated transaction.
- **Not a hardware object.** Nothing is emitted for it. It is the model of whoever decided the
  layout, which in a real system is software.
