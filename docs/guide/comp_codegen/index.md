---
title: Component Code Generation
parent: Guide
nav_order: 7
has_children: true
audience: hls
api: [check, generate, potential_targets]
summary: "Waveflow generates HLS and related C++ from certain HwComponents — automatically for the mechanical parts (top function, AXI pragmas, regmap struct, testbench harness), semi-automatically where you supply the compute body as a hook. Each distinct code output is a target; this section covers the two that are built (control_driven_kernel, sequential_vitis_tb) and names the five that are not. check(source, target) answers whether a given component would lower, running the same rules generate does."
---

# Component Code Generation

A key feature of Waveflow is that it generates HLS and related code from **certain**
[`HwComponent`s](../flows/components.md) — you write the component once in Python (its ports, its parameters,
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
| `control_driven_kernel` | [`HostActivated`](../flows/components.md) | **Built** |
| `sequential_vitis_tb` | [`SeqTB`](../flows/components.md) | **Built** |
| `free_running_kernel` | [`FreeRunComp`](../flows/components.md) | Named, not implemented |
| `composite_kernel` | [`CompositeComp`](../flows/components.md) | Named, not implemented |
| `sequential_xsi_tb` | — | Named, not implemented |
| `concurrent_systemc_tb` | — | Named, not implemented |
| `bitstream` | — | Named, not implemented |

**This section describes the two that are built.** The other five are *named* rather than silent, which
is what lets `check()` answer precisely — see below. They are the future work of
the [free-running and bitstream flows](../flows/); nothing generates them today.

> **`potential_`, not `supported_`.** A class declares the paths that exist **for its kind** — not a
> promise about any particular component. Whether *this* component actually makes it down one is
> `check()`'s answer, not the class's. Synthesizability is a codegen axis, not a class fact
> ([taxonomy](../flows/components.md)).

## Validation and generation

Generating a target takes one input — a **source**. A source is a Python class: the `HwComponent`
you want realized (`SimpFunComponent`), or the [`SeqTB`](../flows/components.md) that drives it
(`SimpFunTBHls`). You never name a method; the entry follows from the component's *kind*
(`HostActivated` → `on_start`, `FreeRunComp` → `run_iter`, `SeqTB` → `main`, and a `CompositeComp` has
no body at all — its codegen is the sub-component graph).

Generation is then two steps over the same `(source × target)` pair:

```
validate(source, target)   — can this source be lowered to this target?
emit(source, target)       — write the C++

generate(source, target)  =  validate + emit
check(source, target)     =  validate          -> (ok, err_msg)
```

Splitting them is what makes codegen **fail-loud**: the source is inspected first, and one that cannot
be lowered raises rather than quietly emitting something wrong. Where possible the error names the
actual problem and the fix — not just *"cannot synthesize"*.

`check` is the same validation with the exception turned into a verdict. It is what makes *"certain
`HwComponent`s"* precise rather than folklore — you can ask:

```python
>>> from waveflow.build.codegen_check import check
>>> check(SimpFunComponent)
(True, None)
>>> check(SimpFunComponent, "concurrent_systemc_tb")
(False, "'concurrent_systemc_tb' is not a potential target for SimpFunComponent; ...")
```

**`check` knows no rules of its own.** It runs the *real* validation, throws the result away, and
reports what happened — so it cannot claim a rule codegen does not enforce, nor miss one it does. A
separate "lightweight" checker would drift; running the same code is the design. The rules live in the
[Extractor](./extractor.md), and adding one there makes `check` report it for free.

### What validation does not cover

Validation checks the parts Waveflow generates. It says nothing about the parts you write:
a **[custom hook](../custom_hooks/) body is never verified** — codegen emits its declaration and a
stub, and the C++ you fill in is yours. Nothing checks that it matches the Python it was derived from,
or that it is correct at all. That is what the example's C-simulation and co-simulation are for.

## In this section

**Start with [Automatic vs. manual](./automatic.md)** — where the generator stops and you begin — then
[Component structure](./structure.md) for how a component becomes a kernel, and
[Extractor](./extractor.md) for what your body may contain. Those three are what you need to *write* a
component. The rest is reference: reach for it when you look inside the generated C++, which mostly
means when you write a [hook](../custom_hooks/).

- [Automatic vs. manual](./automatic.md) — what codegen writes and what you write: everything structural is generated; the compute inside a `@synthesizable` hook is yours.
- [Component structure](./structure.md) — how an `HwComponent` becomes a Vitis HLS top-level function: the kernel entry, the execution model, where hooks come from, and the **contract** for when a component lowers at all.
- [Endpoint interfaces](./interface.md) — how each declared endpoint (stream / m_axi / regmap) is realized as a Vitis port (`hls::stream` / `m_axi` / `s_axilite`) and how a slave endpoint's handler binds.
- [Extractor](./extractor.md) — the synthesizable subset: what the rules are, why each exists, and `check` as their callable form.
- [Codegen](./codegen.md) — how `kernel_files_to_str` emits the deterministic kernel file set and resolves naming.
- [Templating](./templating.md) — the C++ realization of [parameterization](../flows/parametrization.md): how `HwParam` lowers (concrete widths / `.tpp` template params), `HwConst` (deferred), and how `param_supports` emits variant kernels.
- [Testbench](./testbench.md) — how a [`SeqTB`](../flows/components.md)'s `main()` lowers to a `sequential_vitis_tb`.

## See also

- [Realization Flows](../flows/) — the end-to-end *recipe* per target: which build steps run, in what order, and how the result is verified. This section is the per-target mechanics; that section is the story.
- [Hardware Components](../flows/components.md) — the Python `HwComponent` this section generates C++ for.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
- [Build System](../build/) — the `BuildDag` that drives these codegen steps end to end.
