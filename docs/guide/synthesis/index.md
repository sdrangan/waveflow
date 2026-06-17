---
title: Synthesis
parent: Guide
nav_order: 7
has_children: true
---

# Synthesis

Waveflow synthesis converts a Python `HwComponent` into concrete Vitis-ready C++ by walking a fixed build pipeline. The flow starts from a Python class definition and resolves it into a typed intermediate representation (`HwStmt`) that can be emitted deterministically.

The pipeline is organized around five stages: extractor, IR resolution, emitter, build-step orchestration, and timing validation around cosim outputs. The result is a consistent file set (`.hpp`, `.cpp`, and hook impl files) plus structured artifacts for downstream checks.

## In this section

- [Extractor](./extractor.md) — how `HwStmtExtractor` parses synthesizable Python into IR.
- [Codegen](./codegen.md) — how `kernel_files_to_str` emits kernel files and naming.
- [Templating](./templating.md) — how `HwParam` values map to template-aware code paths.
- [Param supports](./param_supports.md) — how variant kernels are emitted from `param_supports`.
- [Testbench](./testbench.md) — `HwTestbench` and `is_testbench=True` codegen mode.
- [Cosim timing](./cosim_timing.md) — extraction and validation of cycle timing artifacts.
