---
title: Code Generation
parent: HLS
grand_parent: Data Schemas
nav_order: 1
audience: hls
api: [DataSchemaStep, ArrayUtilsStep]
summary: "How a Python schema generates a Vitis HLS C++ struct — the .h / _tb.h headers, the DataSchemaStep / ArrayUtilsStep build steps, and the field-by-field struct mapping."
---

# Auto-generating Vitis HLS Files

A key feature of Waveflow data schemas is automatic generation of Vitis-compatible C++ headers from Python schema definitions. For a full build walkthrough, see the [Build System guide](../build/).

## What gets generated

For each schema class, two files are produced:

- `<schema_name>.h` — synthesizable header used in kernel code.
- `<schema_name>_tb.h` — testbench companion header with file I/O/JSON helpers.

For array element helpers, `ArrayUtilsStep` generates:

- `<elem>_array_utils.h`
- `<elem>_array_utils_tb.h`

## Current build flow (`DataSchemaStep` + `ArrayUtilsStep`)

Schema headers and array helpers are generated through `BuildDag` steps. For example, suppose we have a simple [DataList](../python/datalists.md) schema like:

```python
class PolyCmdHdr(DataList):
    """Command header: ...
    """
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, 
            "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,
            "description": "Transaction ID"},
        "nsamp":    {"schema": NsampField,       
             "description": "Sample count (0 for END)"},
    }
```

Then we create the include files in Python with these `BuildStep`s:

```python
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.arrayutils import ArrayUtilsStep

cfg = BuildConfig(root_dir=example_dir)
dag = BuildDag()
dag.add(StreamUtilsStep(output_dir="include"))
dag.add(DataSchemaStep(PolyCmdHdr, word_bw_supported=[32, 64], include_dir="include"))
dag.add(ArrayUtilsStep(Float32, [32, 64]))
dag.run(cfg)
```

The two steps have distinct jobs: **`DataSchemaStep`** generates the header for a schema *class* (here `PolyCmdHdr`), while **`ArrayUtilsStep`** generates the packing helpers for a `DataArray` *element type* (here `Float32`) — see [Serialization](./serialization.md). `BuildConfig(root_dir=...)` sets the build root, and both steps resolve their shared dependency on `StreamUtilsStep` automatically through the DAG.

Running these commands will generate header files: `poly_cmd_hdr.h` and `poly_cmd_hdr_tb.h`.  In the file `poly_cmd_hdr.h`, you will find a structure:

```c
struct PolyCmdHdr {
    PolyCmdType cmd_type;  // DATA or END
    ap_uint<16> tx_id;  // Transaction ID
    ap_uint<16> nsamp;  // Sample count (0 for END)
    
    ...
}
```

In this way, the Python data structure is faithfully translated to the Vitis HLS code — the field types (`PolyCmdType`, `ap_uint<16>`) and even the descriptive comments come straight from the schema. Now any other Vitis C++ file can `#include` the header and declare an instance:

```cpp
#include "include/poly_cmd_hdr.h"

PolyCmdHdr hdr;
hdr.cmd_type = PolyCmdType::DATA;   // the generated enum
hdr.tx_id    = 42;
hdr.nsamp    = 1024;
```

The struct stays in lock-step with its Python definition: change a field there and the regenerated header changes with it, so the kernel can never drift from the source of truth.



## Reading and writing over a channel

Beyond its data members, each generated header also carries **serialization** methods — templated on the channel width `word_bw` — for moving the schema in and out of an AXI-Stream or `m_axi` port. Because they are templated, switching bus width is usually a one-constant change. These methods, and the packing model behind them, are covered in [Serialization & Deserialization](./serialization.md).
