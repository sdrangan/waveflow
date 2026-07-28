# Plan: teaching deadlock — `deadlock.md` + a runnable `examples/deadlock/`

## Motivation

Free-running composites are where a student meets deadlock for the first time, and the guide currently
handles it in one paragraph at the end of
[`comp_codegen/freerunning_composite.md`](../docs/guide/comp_codegen/freerunning_composite.md). That
paragraph was, until recently, **wrong in a specific way**: it said *"every stage needs a token per
job"*, which states the fix as if it were the rule. A stage that emits nothing does not deadlock
anything by itself — the stage downstream idles on an empty stream, which is back-pressure working.

Correcting it exposed that the interesting content is a whole topic, not a caveat: *when* does an
imbalance actually bite, why can you not see it in the topology, and — the part that generalizes past
this subject — **why deadlock looks like success**.

## The one lesson worth taking away

> `sim.run_sim()` returns cleanly when every process is blocked. There is nothing left to schedule, so
> a **deadlocked** run and a **finished** run are indistinguishable by return value.

The repo already knows this and says so in passing —
`tests/examples/test_toy.py` asserts an output count with the comment *"the sim went quiet early
(deadlock looks like a clean finish to `run_sim()`)"*. Under XSI the shape is identical: the cycle
bound elapses and output is simply missing.

So the lesson is not "avoid deadlock". It is **assert counts, never completion**. A test that checks
`len(received) == len(sent)` catches it; a test that checks "it ran without raising" never will. That
is worth teaching early because it generalizes to everything else in the framework.

## The three examples, in teaching order

### 1. `unseeded_loop` — structural, visible, deadlocks at cycle zero

Two stages wired in a cycle, no conditionals, no data dependence:

```
A: x = fwd_in.get();  back_out.write(x)
B: y = back_in.get(); fwd_out.write(y)
```

with `A ──fwd──▶ B ──back──▶ A`. Both block on their first `get`, both streams are empty, and nothing
can ever put the first token in.

Teach this first because the cause is **in the diagram** — you can point at the cycle — and because the
fix teaches the concept: someone must seed the loop, or one stage must write before it reads. That is
what a credit scheme *is*, which sets up example 3.

### 2. `seeded_loop` — one line different, works

The same graph with an initial token injected. Runs to completion. The point is how small the
difference is, and that nothing in the *structure* distinguishes the two.

### 3. `skipping_stage` — seeded, correct for a while, then stops

A stage that skips its return on some jobs. Runs correctly for the first N, then the credits run out
and the upstream stage blocks. Structurally identical to example 2 — the imbalance is only in the
**counts**, and only on some data.

This is the one that bites in practice, and the one that passes C-simulation on one vector and hangs in
RTL on another.

## What the page must NOT say

Do not repeat the corrected mistake. The rule is:

- a stage emitting nothing is **fine** on its own — downstream idles, which is back-pressure;
- it deadlocks when something does **per-job accounting**: a counted completion token, anything
  flowing *backwards* (credits, returned buffers, recycled blocks), or a second input that must stay in
  lockstep;
- the insidious case is a **data-dependent** imbalance — conditionally *acquiring* an SOB lock rather
  than conditionally forwarding after one.

The framework has needed the fix twice, and both are worth citing as real: an un-paced pipeline
deadlocking at `done = N+1`, and a relay that read when handed zero bursts to forward (fixed with an
`if (nfwd > 0)` guard).

## Gates

Each example gets a test asserting the **observed count**, not that it ran:

| example | assertion |
|---|---|
| `unseeded_loop` | `run_sim()` returns AND zero outputs were produced — the demonstration that a clean return proves nothing |
| `seeded_loop` | all N outputs |
| `skipping_stage` | exactly the number of jobs before the credits ran out — pinned, so the failure point is a fact rather than a story |

The first is the important one: it is a test whose *subject* is that the obvious test would have passed.

**XSI half (optional, second pass).** Running `unseeded_loop` through the XSI flow would show the same
symptom in the other backend — cycle bound elapses, output missing — making "looks like success in
both" demonstrated rather than asserted. Worth doing, not worth blocking the page on.

## Scope note

Keep the modules tiny — two stages, one word per job, no schemas beyond a bare word. The subject is the
*wiring*, and every line spent on payload is a line the student has to discount. `examples/toy` is the
size to aim for.

## Related

- [`docs/guide/comp_codegen/freerunning_composite.md`](../docs/guide/comp_codegen/freerunning_composite.md)
  — the corrected trap section this expands
- [`docs/guide/comp_codegen/freerunning_override.md`](../docs/guide/comp_codegen/freerunning_override.md)
  — hand-written bodies, where the framework's own two instances of this bug lived
- [`one_component_two_flows.md`](one_component_two_flows.md) — the free-running execution model
