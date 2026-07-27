---
title: Using Memory in Python
parent: Memory Modeling
nav_order: 4
has_children: false
---

# Using Memory in Python

This page shows how to create and use a `Memory` object in Python. The [histogram example](../../../examples/shared_mem/hist_build.py) provides the running example throughout.

## Creating a `Memory` object

```python
from waveflow.hw.memory import AddrUnit, Memory

mem = Memory(
    word_size=32,       # width of one memory word in bits
    addr_size=64,       # width of addresses in bits (informational)
    addr_unit=AddrUnit.byte,  # byte-addressed (AXI4 style)
)
```

The histogram demo uses exactly this configuration:

```python
# From hist_build.py
MEM_DWIDTH = 32
MEM_AWIDTH = 64
MEM_AUNIT  = AddrUnit.byte

mem = Memory(
    word_size=MEM_DWIDTH,
    addr_size=MEM_AWIDTH,
    addr_unit=MEM_AUNIT,
)
```

### Choosing `word_size`, `addr_size`, and `addr_unit`

| Parameter   | Guidance                                                                                      |
|-------------|-----------------------------------------------------------------------------------------------|
| `word_size` | Match the data width of the AXI or BRAM interface (`mem_dwidth` in the HLS kernel).           |
| `addr_size` | Match the address width of the interface (`mem_awidth`). Used for documentation; does not limit the address space unless `nwords_tot` is also set. |
| `addr_unit` | Use `AddrUnit.byte` when the interface is AXI4 byte-addressed. Use `AddrUnit.word` for local arrays or BRAM-indexed interfaces. |

## Allocating memory regions with `alloc()`

`alloc(nwords)` reserves `nwords` contiguous words and returns the starting address:

```python
addr = mem.alloc(nwords)
```

The return value is expressed in the configured `addr_unit`. With `AddrUnit.byte`, it is a byte address; with `AddrUnit.word`, it is a word index.

Allocation uses first-fit placement: the new segment is placed immediately after all existing segments. This makes allocation order deterministic, which lets the C++ testbench reproduce the same layout.

The histogram demo allocates three regions — input data, bin edges, and output counts:

```python
# From hist_build.py (HistTest.simulate)
nwords_data   = get_nwords(elem_type=Float32, word_bw=mem.word_size, shape=data.shape)
nwords_edges  = get_nwords(elem_type=Float32, word_bw=mem.word_size, shape=bin_edges.shape)
nwords_counts = get_nwords(elem_type=Uint32Field, word_bw=mem.word_size, shape=nbins)

data_addr  = mem.alloc(nwords_data)
edge_addr  = mem.alloc(nwords_edges)
count_addr = mem.alloc(nwords_counts)
```

`get_nwords` computes the number of words needed to hold an array of a given element type and shape at the specified word bit-width.

## Serializing arrays with `write_array()` and `mem.write()`

Data is written to memory in two steps:

1. **Pack** the NumPy array into memory words using `write_array`.
2. **Write** the packed words into the allocated region using `mem.write`.

```python
from waveflow.hw.arrayutils import write_array

packed = write_array(data, elem_type=Float32, word_bw=mem.word_size)
mem.write(data_addr, packed)
```

The histogram demo writes both inputs:

```python
# From hist_build.py (HistTest.simulate)
mem.write(
    data_addr,
    write_array(data, elem_type=Float32, word_bw=mem.word_size),
)
mem.write(
    edge_addr,
    write_array(bin_edges, elem_type=Float32, word_bw=mem.word_size),
)
```

## Reading words with `mem.read()`

`read(addr, nwords)` returns a copy of the requested slice as a NumPy array:

```python
words = mem.read(addr, nwords=nwords_data)
```

The histogram demo reads back the output counts after the accelerator model has run:

```python
# From hist_build.py (HistTest.simulate)
count_words = mem.read(
    count_addr,
    nwords=get_nwords(elem_type=Uint32Field, word_bw=mem.word_size, shape=nbins),
)
```

## Deserializing with `read_array()`

After reading the raw words, use `read_array` to unpack them back into a typed NumPy array:

```python
from waveflow.hw.arrayutils import read_array

counts = read_array(
    count_words,
    elem_type=Uint32Field,
    word_bw=mem.word_size,
    shape=nbins,
)
```

The full read-back sequence in the histogram demo:

```python
# From hist_build.py (HistTest.simulate)
count_words = mem.read(
    count_addr,
    nwords=get_nwords(elem_type=Uint32Field, word_bw=mem.word_size, shape=nbins),
)
counts = read_array(
    count_words,
    elem_type=Uint32Field,
    word_bw=mem.word_size,
    shape=nbins,
)
```

## Freeing a segment with `free()`

`free(addr)` releases a previously allocated segment by its starting address:

```python
mem.free(data_addr)
```

Only the exact starting address returned by `alloc()` is accepted. Passing any other address raises `KeyError`.

## Common pitfalls

### Alignment errors with byte addressing

With `AddrUnit.byte`, every address must be aligned to the word size. The word size in bytes is `word_size // 8`. Addresses produced by `alloc()` are always aligned, but if you compute an address manually (for example, by adding an offset), ensure the result remains aligned:

```python
# BAD: 4-byte offset is fine for 32-bit words, but a 1-byte offset is not
bad_addr = data_addr + 1     # raises ValueError on read/write

# GOOD: offset in whole words, converted to bytes
good_addr = data_addr + 1 * (mem.word_size // 8)
```

### Confusing byte addresses with word indices

`alloc()` returns a byte address when `addr_unit=AddrUnit.byte`. Passing that value directly as a word index (for example, to index into a NumPy array) is wrong:

```python
# WRONG: data_addr is a byte address, not a word index
word_index = data_addr                    # incorrect if addr_unit is byte

# CORRECT: convert byte address to word index
word_index = data_addr // (mem.word_size // 8)
```

In the HLS kernel, the `memmgr::byte_addr_to_word_index<mem_dwidth>` helper performs this conversion. See the [Vitis page](./vitis.md) for details.

### Reads and writes that exceed segment bounds

`read()` and `write()` raise `ValueError` if the requested range extends past the end of the allocated segment. Always use `get_nwords` to compute the correct count before calling these methods.
