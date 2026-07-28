---
title: Block FIR (state + fixed point)
parent: Examples
nav_order: 8
has_children: true
---
# Block FIR — a stateful accelerator, in fixed point, built two ways

This example builds on [`interleaver`](../interleaver/). That one added a real **compute** stage to a
data mover; every firing of it was still *independent* — a gather reads its inputs, writes its outputs,
and remembers nothing. `fir_block` is the first design in the tree where that stops being true.

A block FIR filters a signal `y[i] = Σₖ h[k]·x[i−k]` one **block** at a time. Two things therefore have
to survive from one firing to the next:

1. the **coefficients** `h`, loaded once by a `LOAD_TAPS` command and read by every `FILTER` after it;
2. the **tail** of the previous block — the last `T−1` samples — because `y[0]` of a block needs samples
   that arrived in the *previous* one.

Neither is a buffer passed between components, and neither is memory on the far side of a bus. Both are
storage the module *owns*, declared with [`add_state`](../../guide/memory/hwstate.md). That is the
headline of this example, and it is why the design is a filter rather than something smaller: it needs
**two flavours of state with different lifetimes in one module**, so no single-flavour toy can stand in
for it.

Two other things come with the territory. A filter is arithmetic, so this is the example where the
[fixed-point](../../guide/schema/python/fixpoint.md) story is told end to end — one format for samples,
coefficients and output, and an accumulator that is **derived** rather than hand-sized. And a filter is
a *loop*, so it is the natural place to show that the same specification can be realized more than one
way: `fir_block` ships **two hand-written kernels** computing bit-identical results, selected by one
parameter, and measures what each costs.

## On the word "firing"

This example needs two words that are easy to conflate, so they are used strictly throughout:

- A **firing** is one execution of a task body — one job, one command. State persists *across firings*.
- An **iteration** is one trip of a *loop inside* a body. The two kernels here differ in what happens
  *per iteration* of the filter loop.

They are independent axes: `unroll_lane` changes the iteration structure and does not change what a
firing is, while `add_state` changes what survives a firing and does not change any loop. Every other
page in the guide uses the words this way — see
[free-running codegen](../../guide/comp_codegen/freerunning.md), where a task body is defined as *one
firing rather than a loop*.

## Learning Objectives

In going through this example, you will learn to:

- Give a hardware module **memory between firings** with
  [`add_state`](../../guide/memory/hwstate.md) — both *load-once, held* state and *per-firing carry*
  state, add add **declared reset path** 
- Perform a common **DSP calculation in fixed point** with one declared format, leveraging the
  [format algebra](../../guide/schema/python/fixpoint.md) to *derive* the full-precision accumulators, adding lossy steps where necessary.
- **Hand-write kernel task bodies with different unrolling structures** — one output per iteration
  versus a whole lane per iteration — and create a compile-time parameter to select between the options
- **Verify stateful hardware**, using a golden that is deliberately *stateless* so that agreeing with it
  *is* the proof the state is right, plus falsification tests that break each flavour of state in turn
- **Identify** the firing patterns that can wedge a free-running pipeline — a stage that consumes
  without emitting, or a zero-length transfer handled as if it were non-zero — and **avoid** them by
  keeping every stage's token count uniform across opcodes, so that even a no-output command issues a
  (zero-length) transfer and lands its completion
- **Sweep the bitwidth parameters** and measure the resource and throughput consequences of each
  realization — the DSP packing cliff, and the trade between area and rate

## In this example

The pages build the design up from Python, parallel to [`interleaver`](../interleaver/):

- [Module overview](./firblock.md) — the filter, the four stages, one leaf dispatching two opcodes, and
  why the tap load deliberately does *not* overlap the compute.
- [Cross-firing state](./state.md) — the two flavours, `add_state`, where the `static` lands in a
  free-running task, the declared reset path, and the evidence that it survives in real RTL.
- [Fixed point](./fixedpoint.md) — one format for everything, the derived accumulator, why the window
  reduction is `fixed_sum` and never a loop of `add`, and the width ceiling that lives in Python.
- [Python](./python.md) — building the design: the command/descriptor split, samples versus words, and
  the composite.
- [Testbench (Python)](./testbench.md) — the stateless golden, and the falsification tests that prove
  the gate can fail.
- [The two kernels](./kernels.md) — the serial and unrolled task bodies, the shared delay line, and the
  seeding rule that has bitten this kernel twice.
- [DUT codegen](./codegen_dut.md) — the composite top, how `unroll_lane` selects a body, and the
  storage declarations generated straight from `add_state`.
- [RTL simulation](./rtlsim.md) — the generated BFM harness, the run, and bit-exactness for *both*
  realizations against one golden.
- [Parameter sweep](./sweep.md) — resources and throughput against sample width, for both kernels.
