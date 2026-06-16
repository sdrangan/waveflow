---
title: Test Data Files
parent: HLS
grand_parent: Data Schemas
nav_order: 3
audience: hls
api: [write_uint32_file, read_uint32_file, read_uint32_file_len, write_uint32_file_len, read_uint32_file_array, write_uint32_file_array]
summary: "Move test data between the Python golden and the Vitis C-sim testbench via little-endian uint32 binary files — write_uint32_file / read_uint32_file on the Python side, streamutils::{read,write}_uint32_file on the C++ side — both using the same 32-bit-word packing, so values round-trip bit-for-bit."
---

# Test Data Files (Python ↔ Vitis)

To validate a generated kernel, the Vitis C-simulation testbench needs the **same** test vectors the Python
model uses, and its outputs must be comparable to the Python golden. Waveflow bridges the two with a shared
on-disk format: a **little-endian `uint32` binary file**.

A test-data file is just a schema (or array) **serialized into 32-bit words** — the exact packing from
[Serialization](./serialization.md) at `word_bw = 32`, written as little-endian bytes. Because the Python
writer and the C++ reader use that one packing rule, a value round-trips **bit-for-bit**: Python generates
the inputs, the Vitis testbench reads them, runs the kernel, writes its responses, and Python reads those
back to compare against the golden.

## Python side

A **schema instance** carries the file methods directly:

```python
hdr = PolyCmdHdr(); hdr.tx_id = 42; hdr.nsamp = 1024
hdr.write_uint32_file("data/cmd_hdr.bin")               # serialize -> LE uint32 words on disk

rx = PolyCmdHdr().read_uint32_file("data/cmd_hdr.bin")  # read back; rx == hdr, bit-for-bit
```

- `inst.write_uint32_file(file_path) -> Path` — serialize `inst` to 32-bit words and write them.
- `inst.read_uint32_file(file_path) -> <Schema>` — read the words back and deserialize (returns the instance).

A **raw array** uses the free functions in `waveflow.hw.arrayutils` — you supply the element type:

```python
from waveflow.hw.arrayutils import write_uint32_file, read_uint32_file

write_uint32_file(samples, elem_type=Float32, file_path="data/samp_in.bin", nwrite=1024)
samp = read_uint32_file("data/samp_in.bin", elem_type=Float32, shape=1024)
```

- `write_uint32_file(arr, elem_type, file_path, write_slice=None, nwrite=None) -> Path` — pack `arr`
  (optionally just the first `nwrite` entries, or a NumPy-style `write_slice`) and write it.
- `read_uint32_file(file_path, elem_type, shape) -> array` — read the words and unpack to `shape`.

## Vitis testbench side

The C++ testbench reads and writes the same files through `streamutils` helpers in the generated
`streamutils_tb.h` (emitted by `StreamUtilsStep`; see [Code Generation](./codegen.md)). These are
**testbench-only** — host file I/O, not synthesizable:

```cpp
#include "include/poly_cmd_hdr.h"
#include "include/streamutils_tb.h"

PolyCmdHdr hdr;
streamutils::read_uint32_file(hdr, "data/cmd_hdr.bin");    // file -> schema (calls hdr.read_array<32>)
// ... run the kernel ...
streamutils::write_uint32_file(resp, "results/resp.bin");  // schema -> file
```

- `streamutils::read_uint32_file(value, file_path)` / `write_uint32_file(value, file_path)` — a fixed-size
  schema or array.
- `streamutils::read_uint32_file_len(value, file_path, n0)` / `write_uint32_file_len(value, file_path, n0)`
  — for a schema whose array length is a runtime `n0`.

For a **raw array** element type, the generated `<elem>_array_utils_tb.h` (from `ArrayUtilsStep`) carries the
array-level pair:

```cpp
#include "include/float32_array_utils_tb.h"
namespace au = float32_array_utils;

au::value_type samp[1024];
au::read_uint32_file_array(samp, "data/samp_in.bin", 1024);    // file -> raw array
au::write_uint32_file_array(out,  "results/out.bin",  1024);   // raw array -> file
```

## The round-trip in a build flow

The two sides meet in a build: a Python step writes the input vectors (so the same bytes feed *both* the
SimPy model and Vitis), the C-sim testbench consumes them and emits responses, and a verify step reads both
back and asserts they match the golden bit-for-bit. The poly accelerator's
[Python simulation step](../../build/python.md) is the worked example of the Python side.

## See also

- [Serialization](./serialization.md) — the 32-bit-word packing these files store.
- [Code Generation](./codegen.md) — `StreamUtilsStep` (emits `streamutils_tb.h`) and the `_tb.h` companions.
- [Python Simulation Pattern](../../build/python.md) — `write_uint32_file` / `read_uint32_file` in a real `BuildStep`.
