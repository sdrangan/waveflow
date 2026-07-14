---
title: Component Code Generation
parent: Guide
nav_order: 7
has_children: true
audience: hls
summary: "How a Python HwComponent is generated into Vitis-ready C++: the HLS realization of a component (top function, endpoint ports, execution model), the HwStmt extractor over the synthesizable subset, the emitted file structure, and HwParam parameterization. Hand-written kernel bodies are Custom Hooks."
---

# Component Code Generation

WaveFlow generates **Vitis-ready C++** for the **synthesizable and testbench**
[kinds of `HwComponent`](../components/taxonomy.md) —
[behavioral](../components/) components model hardware it does not
generate and have no C++. For the ones it *does* generate, you write the component once in Python — its
ports, its parameters, its behavior — and the generator emits a top-level HLS kernel with the right AXI
interfaces, the C++ type lowering, and the build-ready file set, all from that single source.

This chapter covers **what the generator produces automatically**: the structure of a generated
component, how each endpoint becomes an HLS port, the execution model it lowers to, what the
`HwStmt` extractor accepts, and how `HwParam` parameterizes the output.

A component's compute body is *not* always auto-generated. When a datapath needs hand-tuned HLS C++,
you write the kernel body yourself and attach it with `@synthesizable(impl_file=…)`. That
hand-written path — how to author a kernel body, the in-kernel port and loop patterns — is the next
chapter, [Custom Hooks](../custom_hooks/). This chapter is the auto-generated structure those hooks
plug into.

## In this section

- [Component structure](./structure.md) — how an `HwComponent` becomes a Vitis HLS top-level function: the kernel entry, the execution model (free-running vs. regmap-launched), and where hooks come from.
- [Endpoint interfaces](./interface.md) — how each declared endpoint (stream / m_axi / regmap) is realized as a Vitis port (`hls::stream` / `m_axi` / `s_axilite`) and how a slave endpoint's handler binds.
- [Extractor](./extractor.md) — how `HwStmtExtractor` parses the synthesizable Python subset into the `HwStmt` IR, failing fast on unsupported patterns.
- [Codegen](./codegen.md) — how `kernel_files_to_str` emits the deterministic kernel file set and resolves naming.
- [Templating](./templating.md) — the C++ realization of [parameterization](../components/parameterization.md): how `HwParam` lowers (concrete widths / `.tpp` template params), `HwConst` (deferred), and how `param_supports` emits variant kernels.
- [Testbench](./testbench.md) — `HwTestbench` and `is_testbench=True` codegen mode.

## See also

- [Hardware Components](../components/) — the Python `HwComponent` this section generates C++ for.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
- [Cosim timing](../timing/cosim_timing.md) — extracting and validating cycle timing from the generated kernel's cosim (now under Timing Analysis, where the timeline analysis lives).
- [Build System](../build/) — the `BuildDag` that drives these codegen steps end to end.
