 

---
title: Component structure
parent: Component Code Generation
nav_order: 1
audience: hls
applies_to: [HwModule]
api: [kernel_files_to_str, cpp_kernel_name, extract_kernel, synthesizable, check]
summary: "How an HwModule becomes a kernel: a standalone component generates one top-level function whose arguments are its endpoints; which method is extracted as the body follows from the component's kind (HostActivated -> on_start, standalone FreeRunMod -> run_iter, composite FreeRunMod -> the graph). The entry method IS extracted; @synthesizable hooks are NOT — they are boundaries whose C++ you write. A component lowers iff it is structurally flat and its body passes the extractor, which check() answers."
---
# Component structure

## Concept

Every `HwModule` that generates code generates **one kernel** — a single Vitis HLS **top-level
function**, the unit Vitis synthesizes into an IP block. That is what "kernel" means throughout this
guide.

Its arguments correspond one-to-one to the component's declared **endpoints**; how each endpoint type
becomes a port (`hls::stream` / `m_axi` / `s_axilite`) is [Endpoint interfaces](./interface.md).

The function name defaults to the class name in `snake_case` with a trailing `_component` stripped
(`PolyAccel → poly_accel`), overridable with `cpp_kernel_name: ClassVar[str] = "..."`.

## Who starts the kernel: `ap_ctrl_hs` vs `ap_ctrl_none`

A kernel needs an answer to one question — **who starts it?** Vitis calls the answer the kernel's
*block-level control protocol*, and it is set on the `return` port. Two answers matter here, and the
terms appear throughout this guide:

**`ap_ctrl_hs`** — "handshake". Somebody starts it, once per run. The block carries four control
signals: **`ap_start`** ("go"), **`ap_done`** ("finished"), plus `ap_idle` and `ap_ready`. The caller
raises `ap_start`, the kernel runs to completion, raises `ap_done`, and stops. **This makes the kernel a
function**: arguments in, one run, a return. Who raises `ap_start` is a *separate* question —
[a host over AXI-Lite, or another block on a wire](./interface.md).

**`ap_ctrl_none`** — no handshake at all. There is no `ap_start` and no `ap_done`, because the block is
never started and never finishes: it runs **forever**, consuming its input streams as data arrives and
stalling when they are empty. Its pace comes from back-pressure, not from a caller.

The consequence runs through everything else:

|                                        | `ap_ctrl_hs`                                | `ap_ctrl_none`                                                         |
| -------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------ |
| Is it a function you can call?         | **yes** — that is what a handshake is  | **no** — it never returns                                         |
| Can a testbench just call it?          | yes, and Vitis builds the RTL harness for you | no — the RTL must be driven directly ([free-running, sequentially driven](../flows/freerun_seq.md)) |
| May it carry`m_axi` / `s_axilite`? | yes                                           | **no** — a free-running interface is stream-only                  |
| Which [flow](../flows/) | [Control-driven kernel](../flows/control_kernel.md) | [Free-running, sequentially](../flows/freerun_seq.md) or [concurrently driven](../flows/freerun_conc.md) |

> **Everything Waveflow generates today is `ap_ctrl_hs`.** `ap_ctrl_none` is the
> [`free_running_kernel`](./index.md) target, which is not built — so a `FreeRunMod` currently emits an
> `ap_ctrl_hs` top despite declaring `control_mode = FREE_RUNNING`. That is a real gap, not a subtlety.
>
> And note a component whose entry is `run_proc` rather than `on_start` is **not** thereby
> free-running — it is `ap_ctrl_hs` with its control on raw pins. The two are easy to conflate.

## Which method becomes the kernel body

The entry follows from the component's **kind**. You never name it; the class states it.

| Kind                                               | Entry extracted                                    | Why                                                                   |
| -------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------- |
| [`HostActivated`](../flows/modules.md) | `on_start`                                       | it runs once per launch —`on_start` is the regmap slave's callback |
| [`FreeRunMod`](../flows/modules.md) (standalone) | `run_iter`                                 | *one firing*; the `while True` belongs to the base, not your code |
| [`FreeRunMod`](../flows/modules.md) (composite)  | — (see below)                              | its body comes from the graph, not a method                           |
| plain`HwModule`                               | `on_start` if it has a regmap, else `run_proc` | the un-migrated standalone component — see below                    |

The dispatch is [`codegen_path(comp)`](../../../waveflow/build/codegen_dispatch.py), and a testbench
routes to `main()` instead ([Testbench](./testbench.md)).

**A composite still generates a kernel** — the same single top-level function as any other component.
What differs is only where its *contents* come from: a composite (a
[`FreeRunMod`](../flows/modules.md) with sub-components) declares no body, so instead of extracting
a method, codegen builds the function from the
**sub-component graph** — one `hls::task` per child, one channel declaration per internal edge, and the
boundary endpoints as its ports. Same output shape, different source.

> **The plain-`HwModule` row is not scaffolding.** It is a real shape: a kernel with no regmap, whose
> arguments arrive as ports rather than registers, and whose body is `run_proc`. `block_scale` is one.
> Such a component is `ap_ctrl_hs` on **raw pins** — it is *not* free-running; the two are easy to
> conflate because both extract a non-`on_start` method. Free-running means `ap_ctrl_none`, which is a
> [target](./index.md) nothing generates yet.

## Where the kernel body comes from

This is the distinction that matters most, and it is easy to get backwards:

- **The entry method is extracted.** Its *shape* — the assignments, the `if`s, the endpoint calls — is
  read from source and translated by the [extractor](./extractor.md) into the kernel's C++.
- **`@synthesizable` methods are *not* extracted.** A `@synthesizable` call is a **hook boundary**. The
  generator emits a *declaration* and a **stub**, and **you write the C++ body**. The Python body stays
  as the simulation golden — it is never lowered.

So in `simp_fun`, `on_start` becomes the kernel body, while `compute` becomes a call to a function you
maintain by hand:

```python
def on_start(self) -> ProcessGen[None]:          # <- extracted; becomes the kernel body
    y = self.compute(                             # <- a hook CALL is emitted...
        self.regmap.get("x"),
        self.regmap.get("a"),
        self.regmap.get("b"),
    )
    self.regmap.set("y", y)

@synthesizable
def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
    return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))   # <- ...but NOT this body
```

The C++ for `compute` lives in the checked-in
[`simp_fun_compute_impl.cpp`](../../../examples/regmap/simp_fun_compute_impl.cpp), written by hand.

Where that stub file lands is the *only* thing `impl_file=` changes:

| Form                                  | Body         | Stub file                                          |
| ------------------------------------- | ------------ | -------------------------------------------------- |
| `@synthesizable`                    | hand-written | `<kernel>_<hook>_impl.cpp` (the default)         |
| `@synthesizable(impl_file="x.tpp")` | hand-written | `x.tpp` — a `.tpp` when the hook is templated |

Both are hand-written. Neither lowers your Python. Writing those bodies is
[Custom Hooks](../custom_hooks/); the hook is *the seam where the generator stops*, and it stops there
because a tuned datapath is the part no generator can guess.

Hooks are emitted into a namespace of `<kernel>_impl` by default, so the call site reads
`simp_fun_impl::compute(...)`. Override with `cpp_namespace`.

> **Sim-only members are never synthesized.** `pre_sim` / `post_sim` exist for the Python
> [simulation lifecycle](../sim/simobj.md#its-lifecycle) only. A `@sim_only` method is stronger still:
> the extractor **strips its calls** from the kernel, which is how `self.timeout(...)` models latency in
> simulation and emits nothing at all.

## The contract: when does a component lower?

> A `HostActivated` generates a **standalone Vitis kernel** if and only if
> **(a)** it owns no sub-components or internal interfaces, and
> **(b)** its `on_start` passes the [extractor](./extractor.md)'s rules.

Rule **(a)** is structural: a standalone component becomes *one function*, and a single function has
nowhere to put a sub-component or an internal channel. Emitting one anyway would silently drop them —
so it raises, and the message points at making it a composite, whose codegen *is* the graph. Rule
**(b)** is the body.

The same shape holds for the other kinds, with the composite inverted: it *must* own a graph.

[`check`](./index.md) answers both halves as one verdict — you do not call two things:

```python
>>> from waveflow.build.codegen_check import check
>>> check(SimpFun)
(True, None)
```

## API

- [`kernel_files_to_str(comp_class, output_dir=".", impl_dir=None)`](../../../waveflow/build/hwgen.py) — generate the kernel file contents for a component class.
- [`cpp_kernel_name(comp_class)`](../../../waveflow/build/hwgen.py) — the default top-function name (`CamelCase → snake_case`, drop `_component`).
- [`codegen_path(comp)`](../../../waveflow/build/codegen_dispatch.py) — the kind → entry dispatch above.
- [`extract_kernel(comp)`](../../../waveflow/build/hwcodegen.py) — extract + resolve the kernel `HwStmt`, enforcing both halves of the contract.
- [`@synthesizable`](../../../waveflow/hw/synth.py) — marks a **hook boundary**: a declaration plus a hand-written stub.
- [`check(source, target)`](../../../waveflow/build/codegen_check.py) — the contract as a predicate.

## Quick reference

- One leaf `HwModule` → one top-level kernel function; its args are its endpoints. A composite → a graph.
- The entry method follows from the **kind**, not from an argument: `on_start` / `run_iter` / `run_proc`.
- The **entry is extracted**; a **`@synthesizable` hook is not** — you write its C++, and `impl_file=` only moves the stub.
- `pre_sim` / `post_sim` are never synthesized; `@sim_only` calls are stripped from the kernel.
- A leaf must be flat: no sub-components, no internal interfaces.
- Override the kernel name with `cpp_kernel_name`; the hook namespace (default `<kernel>_impl`) with `cpp_namespace`.
