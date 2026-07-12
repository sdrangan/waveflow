---
title: Code Generation
parent: HLS
grand_parent: Data Schemas
nav_order: 1
audience: hls
api: [StreamUtilsStep, DataSchemaStep, ArrayUtilsStep]
summary: "Generate the Vitis HLS C++ headers from a schema: DataSchemaStep emits a schema's struct header (+ testbench helpers), ArrayUtilsStep emits the array packing helpers for an element type, both atop the shared StreamUtilsStep."
---

# Auto-generating Vitis HLS Files

A key feature of Waveflow data schemas is automatic generation of Vitis-compatible C++ headers from the
Python schema definition — the struct, its field types, and its serialization methods all come straight from
the single source of truth, so the kernel can never drift from the Python model. Headers are produced by
`BuildDag` steps; for the full build walkthrough see the [Build System guide](../../build/).

This page covers **code generation responsibilities** (what files/types get produced). For **build/execution
responsibilities** (how synthesized RTL is compiled and run, including XSI execution), see
[XSI Build Rung](../../build/xsi.md).

## Generating include files for a schema

Suppose we have a simple [DataList](../python/datalists.md) schema:

```python
class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
    }
```

We generate its C++ header with two build steps:

```python
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.dataschema import DataSchemaStep

cfg = BuildConfig(root_dir=example_dir)
dag = BuildDag()
dag.add(StreamUtilsStep(output_dir="include"))
dag.add(DataSchemaStep(PolyCmdHdr, word_bw_supported=[32, 64], include_dir="include"))
dag.run(cfg)
```

The two steps have distinct jobs:

- **`StreamUtilsStep`** emits the shared `streamutils` header — the low-level bit-packing and stream
  primitives (the `streamutils::` namespace) that every generated read/write method calls into. It is a
  shared dependency: the other steps depend on it, and the `BuildDag` wires that automatically.
- **`DataSchemaStep`** generates the header for a schema *class*. For `PolyCmdHdr` it writes two files:
  - `poly_cmd_hdr.h` — the synthesizable struct plus its serialization methods, and
  - `poly_cmd_hdr_tb.h` — a testbench companion with file-I/O / JSON helpers (not synthesizable).

  `word_bw_supported` lists the channel widths the serialization methods are templated for.

In `poly_cmd_hdr.h` you will find the struct:

```c
struct PolyCmdHdr {
    PolyCmdType cmd_type;  // DATA or END
    ap_uint<16> tx_id;     // Transaction ID
    ap_uint<16> nsamp;     // Sample count (0 for END)
    // ... generated serialization methods ...
};
```

The Python structure is translated faithfully — the field types (`PolyCmdType`, `ap_uint<16>`) and even the
descriptive comments come straight from the schema. Any Vitis C++ file can then `#include` the header and
declare an instance:

```cpp
#include "include/poly_cmd_hdr.h"

PolyCmdHdr hdr;
hdr.cmd_type = PolyCmdType::DATA;   // the generated enum
hdr.tx_id    = 42;
hdr.nsamp    = 1024;
```

Change a field in Python and the regenerated header changes with it, so the kernel can never drift from the
source of truth. Beyond its data members, the header also carries **serialization** methods — templated on
the channel width — for moving the schema over an `m_axi` port or a stream; see
[Serialization](./serialization.md).

## Generating array utils

When a schema is the **element type** of a [`DataArray`](../python/dataarrays.md), packing it across a
channel needs a second kind of helper: the **array utilities**. These are free functions, keyed on the
*element type* (not on any one array), that pack and unpack arrays of that element over a channel — the
packing factor, the lanes, the lane loop. One set of helpers serves *every* array of that element, of any
length, so there is no per-array generated code.

They are produced by **`ArrayUtilsStep`**, which you give the element type and the channel widths to support.
For a 32-bit float element:

```python
from waveflow.hw.dataschema import FloatField
from waveflow.hw.arrayutils import ArrayUtilsStep

Float32 = FloatField.specialize(32)

dag.add(StreamUtilsStep(output_dir="include"))   # shared dependency (as above)
dag.add(ArrayUtilsStep(Float32, [32, 64]))        # element type, supported word widths
dag.run(cfg)
```

`ArrayUtilsStep` writes two files:

- `float32_array_utils.h` — the synthesizable `float32_array_utils::` namespace: the geometry constants
  (`pf<>` / `lane_capacity<>` / `get_nwords<>`), the lane methods (`read_array_lane` / `write_array_lane`),
  the element-range `read_array_slice` / `write_array_slice`, and the stream variants.
- `float32_array_utils_tb.h` — non-synthesizable companions (file I/O) for the testbench.

Because the helpers are keyed on the element type, the same `float32_array_utils` serves every `float`
array. Using them in a kernel — the lane loop, with and without pipelining, and the wide-element (`pf = 0`)
case — is covered in [Vectorization](../../vectorization/hls/raw.md).

## See also

- [Serialization](./serialization.md) — moving a single schema value over each interface.
- [Vitis: raw arrays](../../vectorization/hls/raw.md) — *using* the array utils: the lane loop and `read_array_slice`.
- [Build System](../../build/) — the full `BuildDag` walkthrough.
- [XSI Build Rung](../../build/xsi.md) — building/executing synthesized RTL (distinct from codegen).
