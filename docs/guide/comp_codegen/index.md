---
title: Module Code Generation
parent: Guide
nav_order: 7
has_children: true
audience: hls
api: [check, generate, potential_targets]
summary: "Waveflow generates HLS and related C++ from certain HwModules — automatically for the mechanical parts (top function, AXI pragmas, regmap struct, testbench harness), semi-automatically where you supply the compute body as a hook. Each distinct code output is a target; four are built (control_driven_kernel and sequential_vitis_tb for Flow 1, composite_kernel and sequential_xsi_tb for Flow 2) and only bitstream is not. check(source, target) answers whether a given module would lower, running the same rules generate does."
---

# Module Code Generation

A key feature of Waveflow is that it generates HLS and related code from **certain**
[`HwModule`s](../flows/modules.md) — you write the module once in Python (its ports, its parameters,
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
[Hardware modules and Flows](../flows/) and lives in one place in code
([`waveflow/hw/codegen_targets.py`](../../../waveflow/hw/codegen_targets.py)), so the two cannot drift
apart.

Each *kind* of module declares the targets that exist for it, as `potential_targets`:

| Target | Declared by | Flow | Status |
|---|---|---|---|
| `control_driven_kernel` | [`HostActivated`](../flows/modules.md) | 1 | **Built** |
| `sequential_vitis_tb` | [`SeqTB`](../flows/modules.md) | 1 | **Built** |
| `composite_kernel` | [`FreeRunMod`](../flows/modules.md) — leaf *or* composite | 2 | **Built** |
| `sequential_xsi_tb` | a testbench `FreeRunMod` graph | 2 | **Built** |
| `bitstream` | — | 3 | Named, not implemented |

One name, `composite_kernel`, covers a free-running leaf *and* a composite: a leaf is the 1-task
degenerate case, and `composite_top_spec` walks both. There is no separate `free_running_kernel`.

**Four of the five are built** — Flows 1 and 2 end to end. `bitstream` is *named* rather than
silent, which is what lets `check()` answer precisely rather than failing obscurely; it is the future
work of the [bitstream flow](../flows/bitstream_ipi.md).

The single source for this list is
[`waveflow/hw/codegen_targets.py`](../../../waveflow/hw/codegen_targets.py), which also records which
are implemented — so this table and the code cannot drift silently.

> **`potential_`, not `supported_`.** A class declares the paths that exist **for its kind** — not a
> promise about any particular module. Whether *this* module actually makes it down one is
> `check()`'s answer, not the class's. Synthesizability is a codegen axis, not a class fact
> ([taxonomy](../flows/modules.md)).

## Validation and generation

Generating a target takes one input — a **source**. A source is a Python class: the `HwModule`
you want realized (`SimpFun`), or the [`SeqTB`](../flows/modules.md) that drives it
(`SimpFunTBHls`). You never name a method; the entry follows from the module's *kind*
(`HostActivated` → `on_start`, a standalone `FreeRunMod` → `run_iter`, `SeqTB` → `main`, and a
composite `FreeRunMod` has no body at all — its codegen is the sub-component graph).

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
`HwModule`s"* precise rather than folklore — you can ask:

```python
>>> from waveflow.build.codegen_check import check
>>> check(SimpFun)
(True, None)
>>> check(SimpFun, "bitstream")
(False, "'bitstream' is not a potential target for SimpFun; its potential targets are {'control_driven_kernel'}")
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

The section is **one page per target, then the shared mechanism**.
[Module structure](./structure.md) is the frame all four share; then
[host-activated](./hostactivated.md), [free-running](./freerunning.md),
[sequential testbench](./testbench.md), and [XSI testbench](./xsi_tb.md) are how each is realized.
The pages after those — interfaces, extractor, generated files, templating — are the machinery they
have in common. How to *describe* a module in Python is the previous section,
[Hardware modules and Flows](../flows/).

**Start with [Automatic vs. manual](./automatic.md)** — where the generator stops and you begin — then
[Module structure](./structure.md) for how a module becomes a kernel, and
[Extractor](./extractor.md) for what your body may contain. Those three are what you need to *write* a
module. The rest is reference: reach for it when you look inside the generated C++, which mostly
means when you write a [hook](../custom_hooks/).

- [Automatic vs. manual](./automatic.md) — what codegen writes and what you write: everything structural is generated; the compute inside a `@synthesizable` hook is yours.
- [Module structure](./structure.md) — the frame all four targets share: one top-level function per module, which method is extracted for which kind, entry-is-extracted vs hook-is-not, and the **contract** for when a module lowers at all.
- [Host-activated kernel in HLS](./hostactivated.md) — the `control_driven_kernel`: `ap_ctrl_hs`, the `s_axilite` register block, and `on_start` as the body.
- [Endpoint interfaces](./interface.md) — how each declared endpoint (stream / m_axi / regmap) is realized as a Vitis port (`hls::stream` / `m_axi` / `s_axilite`) and how a slave endpoint's handler binds.
- [Free-running kernel in HLS](./freerunning.md) — the `composite_kernel`: the graph-derived `ap_ctrl_none` top, `KernelTask`, the generated task body, and where `HwState` statics land.
- [Extractor](./extractor.md) — the synthesizable subset: what the rules are, why each exists, and `check` as their callable form.
- [Generated files](./codegen.md) — the two file lifecycles (framework-owned `gen/` vs sticky hook impls), `.cpp` vs `.tpp` routing, and naming.
- [Templating](./templating.md) — the C++ realization of [parameterization](../flows/parametrization.md): how `HwParam` lowers (concrete widths / `.tpp` template params), `HwConst` (deferred), and how `param_supports` emits variant kernels.
- [Sequential testbench](./testbench.md) — how a [`SeqTB`](../flows/modules.md)'s `main()` lowers to a `sequential_vitis_tb`.
- [XSI testbench in HLS](./xsi_tb.md) — how a testbench *graph* lowers to a `sequential_xsi_tb`: participants map to pre-written BFM models, and the scenario lives in burst bundles rather than in the C++.

## See also

- [Hardware modules and Flows](../flows/) — the end-to-end *recipe* per target: which build steps run, in what order, and how the result is verified. This section is the per-target mechanics; that section is the story.
- [Hardware Modules](../flows/modules.md) — the Python `HwModule` this section generates C++ for.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated module.
- [Build System](../build/) — the `BuildDag` that drives these codegen steps end to end.
