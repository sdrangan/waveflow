---
title: Code Generation Steps
parent: Build System
nav_order: 2
summary: "The four built-in steps that turn Python schema definitions into the C++ a Vitis kernel compiles against: the stream helpers, the memory-manager headers, a header pair per DataSchema, and packed-array helpers per scalar element type. The dependency wiring among them is automatic, but the stream helpers must be added first."
---

# Code Generation Steps

Waveflow ships four built-in build steps that generate the C++ headers and helpers a Vitis HLS kernel needs from Python schema definitions. All four are `Buildable` subclasses — the convenience base class for steps that write text-file outputs — and are imported from their respective modules:

| Step | Purpose | Module |
|---|---|---|
| `StreamUtilsStep` | Copy synthesizable + testbench stream helpers into the include directory | `waveflow.build.streamutils` |
| `MemMgrStep` | Copy memory-manager headers into the include directory | `waveflow.build.streamutils` |
| `DataSchemaStep` | Generate the C++ header pair for one `DataSchema` class | `waveflow.hw.dataschema` |
| `ArrayUtilsStep` | Generate packed-array helpers for one scalar element type | `waveflow.hw.arrayutils` |

Each step is typically added once per design (or once per schema, in the case of `DataSchemaStep`), and the dependency wiring among them is automatic.

---

## StreamUtilsStep

Copies the stream-helper C++ files into a chosen include directory. Every `DataSchemaStep` and `ArrayUtilsStep` depends on these headers, so `StreamUtilsStep` must be added to the DAG first.

```python
from waveflow.build.streamutils import StreamUtilsStep

dag.add(StreamUtilsStep(output_dir="include"))
```

| Parameter | Description |
|---|---|
| `output_dir` | Directory relative to `config.root_dir` where the headers are written. Defaults to `"."`. |

Outputs:
- `<output_dir>/streamutils_hls.h` — synthesizable AXI-stream types and serialization primitives.
- `<output_dir>/streamutils_tb.h` — testbench file I/O and JSON helpers.
- `<output_dir>/streamutils.cpp` — companion implementation file. Written **only** when `config.vitis_version_tuple() < (2025, 1)` or `config.vitis_version is None`. If a stale `streamutils.cpp` exists from an older build and the current Vitis version is ≥ 2025.1, the step deletes it.

---

## MemMgrStep

Copies the memory-manager helper headers. Use when a design needs the `memmgr` primitives. Independent of `StreamUtilsStep` — no auto-wiring between them.

```python
from waveflow.build.streamutils import MemMgrStep

dag.add(MemMgrStep(output_dir="include"))
```

| Parameter | Description |
|---|---|
| `output_dir` | Directory relative to `config.root_dir` where the headers are written. Defaults to `"."`. |

Outputs:
- `<output_dir>/memmgr.hpp`
- `<output_dir>/memmgr_tb.hpp`

---

## DataSchemaStep

Generates a pair of C++ headers for one `DataSchema` class:

- `<include_dir>/<schema_name>.h` — synthesizable struct definition + serialization methods.
- `<include_dir>/<schema_name>_tb.h` — testbench file I/O and JSON helpers.

```python
from waveflow.hw.dataschema import DataSchemaStep

dag.add(DataSchemaStep(
    PolyCmdHdr,
    word_bw_supported=[32, 64],
    include_dir="include",
))
```

| Parameter | Description |
|---|---|
| `schema_cls` | The `DataSchema` subclass to generate headers for. |
| `word_bw_supported` | List of word widths (e.g. `[32, 64]`) to generate serialization methods for. |
| `include_dir` | Directory relative to `config.root_dir` where the headers are written. |
| `include_filename` | Override the default output filename (optional). |

### Dependency wiring

When added to a `BuildDag`, `DataSchemaStep` automatically:

1. Wires itself to the `StreamUtilsStep` already in the DAG (required — raises `ValueError` if none is found).
2. Wires itself to any `DataSchemaStep` instances for schema types it depends on (e.g. if `PolyCmdHdr` contains a `CoeffArray` field, it wires to the `DataSchemaStep` for `CoeffArray`).

The `#include` paths in the generated headers automatically point to the correct relative locations based on this wiring.

### Adding steps in dependency order

Schema dependencies must be added before the schemas that reference them. For the poly example:

```python
dag.add(StreamUtilsStep(output_dir="include"))

# Leaves first (no schema dependencies):
dag.add(DataSchemaStep(PolyErrorField, word_bw_supported=[32, 64], include_dir="include"))
dag.add(DataSchemaStep(CoeffArray,     word_bw_supported=[32, 64], include_dir="include"))

# Containers next:
dag.add(DataSchemaStep(PolyCmdHdr,  word_bw_supported=[32, 64], include_dir="include"))
dag.add(DataSchemaStep(PolyRespHdr, word_bw_supported=[32, 64], include_dir="include"))
```

If a `SCHEMA_CLASSES` list is already ordered correctly (leaf types first), the list-comprehension form is concise:

```python
schema_steps = [
    dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir="include"))
    for cls in SCHEMA_CLASSES
]
```

### include_dir vs. class-level include_dir

The step-level `include_dir` takes precedence over any `include_dir` class attribute on the schema. The recommended pattern is to **not** set `include_dir` on the schema class — keep the schema free of build-system concerns and specify the location per-step:

```python
# Preferred: schema class has no include_dir
class PolyCmdHdr(DataList):
    elements = { ... }

# include_dir is specified at the step level
dag.add(DataSchemaStep(PolyCmdHdr, word_bw_supported=[32, 64], include_dir="include"))
```

---

## ArrayUtilsStep

Generates packed-array helper headers for a scalar element type. The output provides C++ functions for reading and writing arrays of that type across AXI streams and arrays at every supported word width.

```python
from waveflow.hw.arrayutils import ArrayUtilsStep

dag.add(ArrayUtilsStep(Float32, [32, 64]))
```

| Parameter | Description |
|---|---|
| `elem_type` | A `DataSchema` subclass for the scalar element type (e.g. `Float32`, `PixelField`). |
| `word_bw_supported` | List of word widths to generate helpers for. |

Outputs:
- `<elem_type.include_dir>/<name>_array_utils.h` — synthesizable helpers.
- `<elem_type.include_dir>/<name>_array_utils_tb.h` — testbench helpers.

The output directory comes from `elem_type.include_dir`, so the element-type specialization should include `include_dir`:

```python
Float32 = FloatField.specialize(bitwidth=32, include_dir="include")
dag.add(ArrayUtilsStep(Float32, [32, 64]))
# writes to include/float32_array_utils.h
```

`ArrayUtilsStep` automatically wires itself to the `StreamUtilsStep` in the DAG.

---

## A note on `Buildable` and rebuild semantics

All four steps above subclass `Buildable` rather than `BuildStep` directly. `Buildable` is the convenience base for steps whose output is "a fixed set of named text files written from string-valued generators." Practical implications for users:

- The DAG cannot mtime-skip Buildable steps — they re-run on every `dag.run()`. This is normally fine because writing a few small text files is cheap and deterministic, and downstream Vitis steps are still skipped on freshness if these outputs land unchanged. If you need finer control, force a single step with `force=["StreamUtilsStep"]` to trigger downstream cascade.
- Buildable steps wire their dependencies via a `resolve_deps()` hook rather than declared `consumes` lists. `DataSchemaStep` and `ArrayUtilsStep` use this to find the `StreamUtilsStep` in the DAG; you don't need to set anything up.

When to subclass `Buildable` vs `BuildStep`:

- `Buildable` is the right choice when the step writes a fixed set of text files generated as strings, has no in-memory artifacts to pass downstream, and doesn't depend on artifact values from prior steps.
- `BuildStep` (with explicit `consumes` / `produces`) is the right choice for anything else — steps that read upstream artifact values, produce in-memory results, mix file and object outputs, or want proper mtime-based freshness. See [Core Components](./corecomp.md#buildstep) for the API.

---

## Complete example

The full codegen sub-DAG for the poly accelerator:

```python
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep

def gen_vitis_code(example_dir, include_dir="include"):
    config = BuildConfig(root_dir=example_dir)
    dag = BuildDag()

    dag.add(StreamUtilsStep(output_dir=include_dir))

    schema_steps = [
        dag.add(DataSchemaStep(cls, word_bw_supported=[32, 64], include_dir=include_dir))
        for cls in [PolyErrorField, CoeffArray, PolyCmdHdr, PolyRespHdr]
    ]

    dag.add(ArrayUtilsStep(Float32, [32, 64]))

    results = dag.run(config)
    return [results[step.name].artifacts["include"] for step in schema_steps]
```

In a larger build that also runs Vitis, this would typically be wrapped in a single `HlsGenIncludeStep(BuildStep)` so the codegen sub-DAG is one node in the outer pipeline — see the [Python Simulation Pattern](./python.md) and [Vitis Pattern](./vitis.md) pages for how that wrapping is done in practice.
