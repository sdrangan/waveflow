---
title: Component Code Generation
parent: Guide
nav_order: 7
has_children: true
audience: hls
summary: "The auto-generated codegen path: how a Python HwComponent lowers to Vitis-ready C++ — the HwStmt extractor over the synthesizable subset, the emitted file structure, and HwParam parameterization (templating / param_supports). Hand-written kernel bodies are Custom Hooks."
---

# Component Code Generation

This section is the **auto-generated codegen path**: how a Python [`HwComponent`](../components/) is
lowered into concrete, Vitis-ready C++. It is the first half of Arc 3 (hardware generation) — the
**machine-generated** structure of a component. The **hand-written** synthesizable kernel bodies that
plug into that structure (the `@synthesizable(impl_file=…)` hooks) are documented in
[Custom Hooks](../custom_hooks/).

The flow starts from a Python class and resolves it into a typed intermediate representation
(`HwStmt`) that is emitted deterministically. Two things define a generated component:

- **What is lowered** — the [extractor](./extractor.md) walks the *synthesizable subset* of the
  component's Python (`on_start` / `run_proc`, or `main` for a testbench) into the `HwStmt` IR, and
  the [emitter](./codegen.md) turns that IR into the `.hpp` / `.cpp` file set (plus sticky hook impl
  files).
- **How it is parameterized** — `HwParam` fields drive [templating](./templating.md) (template-aware
  C++ / `.tpp` hook stubs) and [param_supports](./param_supports.md) (multiple concrete kernel
  entry points from one class).

## Auto-generated vs. hand-written

This section covers only the **auto-generated** C++. The body of a compute kernel is *not* generated
— it is hand-written in a `.tpp` and attached to the component with `@synthesizable(impl_file=…)`.
Those hand-written hooks (how to author one, the contract they satisfy) are documented in
[Custom Hooks](../custom_hooks/).

## In this section

- [Extractor](./extractor.md) — how `HwStmtExtractor` parses the synthesizable Python subset into IR.
- [Codegen](./codegen.md) — how `kernel_files_to_str` emits kernel files and naming.
- [Templating](./templating.md) — how `HwParam` values map to template-aware code paths.
- [Param supports](./param_supports.md) — how variant kernels are emitted from `param_supports`.
- [Testbench](./testbench.md) — `HwTestbench` and `is_testbench=True` codegen mode.

## See also

- [Hardware Components](../components/) — the Python `HwComponent` this section generates C++ for.
- [Cosim timing](../timing/cosim_timing.md) — extracting and validating cycle timing from the generated kernel's cosim (now under Timing Analysis, where the timeline analysis lives).
- [Build System](../build/) — the `BuildDag` that drives these codegen steps end to end.
