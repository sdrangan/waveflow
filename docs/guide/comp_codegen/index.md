---
title: Component Code Generation
parent: Guide
nav_order: 7
has_children: true
audience: hls
api: [check, generate, potential_targets]
summary: "Waveflow generates HLS and related C++ from certain HwComponents — automatically for the mechanical parts (top function, AXI pragmas, regmap struct, testbench harness), semi-automatically where you supply the compute body as a hook. Each distinct code output is a target; this section covers the two that are built (control_driven_kernel, sequential_vitis_tb) and names the five that are not. check(subject, target) answers whether a given component would lower, running the same rules generate does."
---

# Component Code Generation

A key feature of Waveflow is that it generates HLS and related code from **certain**
[`HwComponent`s](../components/) — you write the component once in Python (its ports, its parameters,
its behavior) and the generator emits build-ready C++ from that single source.

It is **automatic** for everything mechanical: the top-level function and its signature, the
`#pragma HLS INTERFACE` directives, the AXI-Lite regmap struct, the C++ type lowering, the testbench
harness. It is **semi-automatic** where the datapath needs hand-tuned HLS: a method marked
[`@synthesizable`](../custom_hooks/) is a **hook boundary**, so the generator emits its declaration and
a `// TODO` stub, and *you* write the body. The generated wrapper is the part you never have to write;
the hook is the part no generator can guess. Authoring those bodies is the next chapter,
[Custom Hooks](../custom_hooks/); this chapter is the structure they plug into.

## Targets

Each distinct code output is a **target**. The vocabulary is shared verbatim with
[Realization Flows](../flows/) and lives in one place in code
([`waveflow/hw/codegen_targets.py`](../../../waveflow/hw/codegen_targets.py)), so the two cannot drift
apart.

Each *kind* of component declares the targets that exist for it, as `potential_targets`:

| Target | Declared by | Status |
|---|---|---|
| `control_driven_kernel` | [`HostActivated`](../components/hostactivated.md) | **Built** |
| `sequential_vitis_tb` | [`SeqTB`](../components/) | **Built** |
| `free_running_kernel` | [`FreeRunComp`](../components/freerun.md) | Named, not implemented |
| `composite_kernel` | [`CompositeComp`](../components/composite.md) | Named, not implemented |
| `sequential_xsi_tb` | — | Named, not implemented |
| `concurrent_systemc_tb` | — | Named, not implemented |
| `bitstream` | — | Named, not implemented |

**This section describes the two that are built.** The other five are *named* rather than silent, which
is what lets `check()` answer precisely — see below. They are the future work of
[Flows 2–4](../flows/); nothing generates them today.

> **`potential_`, not `supported_`.** A class declares the paths that exist **for its kind** — not a
> promise about any particular component. Whether *this* component actually makes it down one is
> `check()`'s answer, not the class's. Synthesizability is a codegen axis, not a class fact
> ([taxonomy](../components/taxonomy.md)).

## One dispatch, two modes

Codegen answers two questions over the same `(subject × target)` pair:

```
generate(subject, target)  =  validate(subject, target) + emit(...)
check(subject, target)     =  validate(subject, target)          -> (ok, err_msg)
```

`generate` is fail-loud: a build wants the traceback. `check` is the **predicate** form of the identical
rules — it is what makes *"certain `HwComponent`s"* precise rather than folklore:

```python
>>> from waveflow.build.codegen_check import check
>>> check(SimpFunComponent)
(True, None)
>>> check(SimpFunComponent, "concurrent_systemc_tb")
(False, "'concurrent_systemc_tb' is not a potential target for SimpFunComponent; ...")
```

**`check` knows no rules of its own.** It runs the *real* extraction, throws the tree away, and turns
the raise into a verdict — so it cannot report a rule codegen does not enforce, nor miss one it does. A
separate "lightweight" checker would be a shadow that drifts; running the same code is the design. The
rules themselves live in the [Extractor](./extractor.md), and adding one there makes `check` report it
for free.

## In this section

- [Component structure](./structure.md) — how an `HwComponent` becomes a Vitis HLS top-level function: the kernel entry, the execution model, where hooks come from, and the **contract** for when a component lowers at all.
- [Endpoint interfaces](./interface.md) — how each declared endpoint (stream / m_axi / regmap) is realized as a Vitis port (`hls::stream` / `m_axi` / `s_axilite`) and how a slave endpoint's handler binds.
- [Extractor](./extractor.md) — the synthesizable subset: what the rules are, why each exists, and `check` as their callable form.
- [Codegen](./codegen.md) — how `kernel_files_to_str` emits the deterministic kernel file set and resolves naming.
- [Templating](./templating.md) — the C++ realization of [parameterization](../components/parameterization.md): how `HwParam` lowers (concrete widths / `.tpp` template params), `HwConst` (deferred), and how `param_supports` emits variant kernels.
- [Testbench](./testbench.md) — how a [`SeqTB`](../components/)'s `main()` lowers to a `sequential_vitis_tb`.

## See also

- [Realization Flows](../flows/) — the end-to-end *recipe* per target: which build steps run, in what order, and how the result is verified. This section is the per-target mechanics; that section is the story.
- [Hardware Components](../components/) — the Python `HwComponent` this section generates C++ for.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
- [Build System](../build/) — the `BuildDag` that drives these codegen steps end to end.
