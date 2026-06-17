---
title: Memory Modeling
parent: Guide
nav_order: 10
has_children: true
---

# Memory Modeling

Waveflow provides a lightweight memory model that lets you simulate AXI-style and local-array-style memory interfaces in Python, then connect those simulations directly to Vitis HLS kernels and testbenches.

## What is a memory model in Waveflow?

A **memory model** is a sparse, word-addressed store that lives in Python during simulation. Rather than pre-allocating a large flat buffer, you allocate only the regions you actually use. Each allocated region is backed by a NumPy array, so the full NumPy ecosystem is available for generating and inspecting data.

The central class is `Memory`, imported from `waveflow.hw.memory`:

```python
from waveflow.hw.memory import AddrUnit, Memory
```

## Key features

- **Sparse allocation** — only regions explicitly allocated with `alloc()` consume memory. There is no large flat backing array.
- **Configurable word size** — `word_size` can be 32, 64, or wider (> 64 bits are stored as `uint64` chunks).
- **Configurable address size** — `addr_size` sets the notional width of addresses in bits.
- **Two addressing models** — byte-addressed (AXI4/DDR style) or word-indexed (local-array style), selected via `addr_unit`.
- **Deterministic first-fit allocation** — `alloc()` always places the next segment immediately after the highest existing segment, making allocation order reproducible across Python and C++ testbenches.
- **Array serialization compatibility** — `Memory.read()` and `Memory.write()` accept NumPy arrays produced by the `write_array` / `read_array` helpers, keeping Python models and HLS kernels consistent.

## Two addressing models

The `AddrUnit` enum controls how addresses are interpreted:

| `addr_unit`        | Meaning of an address                         | Typical use case                    |
|--------------------|-----------------------------------------------|-------------------------------------|
| `AddrUnit.byte`    | Byte offset from the start of the buffer      | AXI4 / DDR / PYNQ memory interfaces |
| `AddrUnit.word`    | Word index into the underlying array          | Local arrays, BRAM-like interfaces  |

### `AddrUnit.byte` — AXI-style byte addressing

With byte addressing, `alloc()` returns a byte address. Addresses must be aligned to the word size (e.g., a multiple of 4 bytes for a 32-bit word). The memory model converts byte addresses to word indices internally:

```
word_index = byte_addr // (word_size // 8)
```

This matches the AXI4 convention used by Vitis HLS `m_axi` ports and PYNQ DDR buffers.

### `AddrUnit.word` — word-indexed addressing

With word addressing, `alloc()` returns a word index. Addresses map directly to indices into the backing NumPy array. This models local arrays and BRAM-like interfaces where the hardware accesses words by index, not by byte address.

## Connecting Python models to Vitis flows

The memory model bridges two sides of a Waveflow flow:

1. **Python simulation** — the Python accelerator model reads inputs from and writes outputs to a `Memory` instance, exactly as the synthesized kernel will access the AXI memory port.
2. **Vitis/HLS testbench** — the C++ testbench uses `MemMgr<word_dwidth>` (from `memmgr_tb.hpp`) to mirror the Python allocation layout. Because both sides use the same first-fit algorithm, byte addresses computed in Python are valid in the C++ testbench as well.

The histogram example demonstrates the full round-trip:
- Python allocates regions and writes packed input arrays.
- The Python accelerator model reads from and writes to those regions.
- The same addresses are written into a command descriptor.
- The C++ testbench reconstructs the same layout, runs the HLS kernel, and writes results back.
- Python reads the results and compares against the reference.

## Next steps

- [Using Memory in Python](./python.md) — how to create a `Memory`, allocate regions, and serialize arrays.
- [Memory Interfaces in Vitis HLS](./vitis.md) — how the memory model maps to `m_axi` and local-array interfaces.
