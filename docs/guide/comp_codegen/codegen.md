---
title: Codegen
parent: Component Code Generation
nav_order: 2
audience: hls
api: [kernel_files_to_str, HlsCodegenStep]
summary: "How resolved HwStmt IR is emitted to deterministic kernel files — <kernel>.hpp / .cpp plus sticky hook impl files — with C++ type lowering, top-level signatures, namespace-qualified hook calls, and variant naming."
---

# Codegen

## Concept

Code generation turns resolved `HwStmt` IR into concrete kernel files and hook stubs. The output is deterministic: every run rewrites `<kernel>.hpp` and `<kernel>.cpp`, while hook impl files are sticky unless missing.

The emitter handles C++ type lowering, top-level function signatures, namespace-qualified hook calls, and variant naming. Top kernels are emitted as concrete functions suitable for Vitis entry points.

## API

- [`kernel_files_to_str(comp_class, output_dir=".", impl_dir=None)`](../../../waveflow/build/hwgen.py) returns generated file contents.
- [`HlsCodegenStep`](../../../waveflow/build/hwcodegen_steps.py) writes generated files into the build output.
- [`cpp_kernel_name(comp_class)`](../../../waveflow/build/hwgen.py) derives kernel names.
- [`resolved_namespace(comp_class)`](../../../waveflow/build/hwgen.py) resolves hook namespace behavior.

## Example

From [`examples/stream_inband/poly_build.py`](../../../examples/stream_inband/poly_build.py), `HlsCodegenStep` is used in the build DAG to emit `poly.hpp`, `poly.cpp`, and impl stubs:

```python
inner_dag.add(HlsCodegenStep(
    comp_class=PolyAccelComponent,
    source_artifact="source_dir",
    output_dir="gen",
    impl_dir="gen",
))
```

Generated files include kernel signatures and stream/regmap mappings based on the component endpoints.

## Quick reference

- File set: `.hpp`, `.cpp`, and per-hook `_impl.{cpp,tpp}`.
- `.tpp` is used when hook template params are required.
- `impl_dir` files are created only when absent (sticky lifecycle).
- Stale `.cpp`/`.tpp` extension mismatches are treated as errors.
- Kernel top functions remain global; hook functions may be namespaced.
