---
title: Automatic vs. manual
parent: Module Code Generation
nav_order: 0
audience: hls
api: [synthesizable]
summary: "Where the generator stops. Everything structural is automatic — the kernel signature, every interface pragma, the regmap struct, the type lowering, the entry body's shape, the testbench harness. What you write by hand is the compute inside a @synthesizable hook, and nothing else. For simp_fun that is 95 generated lines against six you write. A kernel with no hand-tuned datapath (CmdRx) needs nothing manual at all. Over time more of the compute becomes automatic, including via AI agents."
---

# Automatic vs. manual code generation

Waveflow's goal is to generate **as much of a target as it can** from your Python. The realistic
statement today is that everything *structural* is automatic, and one thing is not: the **compute**.

Knowing exactly where that line falls is the most useful thing in this section — it tells you which of
these pages you can skip, and it is the one thing readers reliably get wrong.

## What is automatic

For a kernel and its testbench, codegen writes:

- the **top-level function** and its whole argument list, derived from your endpoints;
- every **`#pragma HLS INTERFACE`** — `axis`, `m_axi`, `s_axilite`, and the control protocol on
  `return` ([Endpoint interfaces](./interface.md));
- the **regmap** struct and its register layout;
- the **C++ type lowering** — each `DataSchema` becomes an `ap_int` / `ap_fixed` / struct, with its
  serialization;
- the **body's shape** — your entry method's assignments, `if`s, loops and endpoint calls, read from
  source and translated ([Extractor](./extractor.md));
- the **stream and memory transactions** — the AXI4-Stream reads and writes, the burst reads/writes;
- the **testbench harness** — `int main()`, the file I/O, the DUT call ([Testbench](./testbench.md));
- the **file set** itself: headers, guards, includes, namespaces, and one kernel per parameter variant.

Concretely, for [`examples/regmap`](../../examples/regmap/): codegen writes **95 lines** across three
files (`simp_fun.hpp`, `simp_fun.cpp`, `simp_fun_tb.cpp`), all rewritten from scratch on every run.

## Where it stops: the compute

What you write is the body of a [`@synthesizable`](../custom_hooks/) **hook** — and, today, that is
all. In `simp_fun` the hook is `compute(x, a, b)`, and the C++ you own is **six lines** of arithmetic.
Ninety-five generated, six by hand.

`@synthesizable` marks a **boundary**: codegen emits the declaration and calls it from the kernel, then
writes a **stub** for the body — once. The stub file is *sticky*: written only if absent, then never
touched again, so your edits survive every rebuild. The Python body of the hook stays as the
simulation golden; it is not lowered.

> This is the point most often misread — including by three pages of this guide, until recently. A
> `@synthesizable` method's Python is **not** translated to C++. Writing it in a "synthesizable style"
> changes nothing about the output; it only keeps the Python model readable next to the C++ you wrote.

**Not every kernel needs a hook.** `CmdRx` in the interleaver has none — it emits `cmd_rx.hpp` and
`cmd_rx.cpp` and nothing else: 26 lines, entirely generated. A component whose body is only structure —
read a command, route it — is fully automatic today. Hooks appear exactly where a *hand-tuned datapath*
does.

## Why the line is there

A hook is where a human decision belongs. The generated wrapper has one correct form — the ports follow
from the endpoints, the pragmas follow from the ports. A datapath does not: the same maths can be a
pipelined loop, an unrolled tree, or a systolic array, and choosing well needs an understanding of the
target that the Python spec does not carry. So Waveflow generates the part with one right answer and
hands you the part with many.

## What changes over time

The line moves toward automatic. More of the control and compute logic becomes generated as the
extractor's [subset](./extractor.md) grows and as more targets are built. And the hook is a natural fit
for an **AI agent**: it is a small, well-typed, well-specified hole — the signature is fixed, the
Python body states the intent, and the example's C-simulation and co-simulation check the result. Every
hook written by hand today is also grounding for that.

For now, expect most kernels to need some hand-written or agent-written compute — and expect that to be
a small fraction of the C++ that ends up in the build.

## Next

- [Module structure](./structure.md) — how a component becomes a kernel, and when it lowers at all.
- [Extractor](./extractor.md) — what your entry method may contain.
- [Custom Hooks](../custom_hooks/) — writing the part that is yours.
