---
title: Module Overview
parent: Block FIR (state + fixed point)
nav_order: 1
---
# Block FIR — a filter that remembers

`fir_block` computes an FIR filter over a stream of blocks:

```
y[i] = Σ h[k]·x[i−k]      k = 0 … T−1
```

Given `T` coefficients `h` and a block of `n` input samples, it produces `n` output samples. The host
issues commands; each command is one **firing** of the pipeline; each firing lands one completion on
`s_done`.

What makes it the next step after [`interleaver`](../interleaver/) is not the arithmetic — it is that a
firing is no longer self-contained.

## Why a filter needs memory between firings

Look at the first output of a block. `y[0]` needs `x[0], x[−1], x[−2], …, x[−(T−1)]` — and all but the
first of those arrived in the **previous** block. Nothing in the current firing's input contains them.

The same is true of the coefficients, for a different reason. `h` is not per-job data; it is
configuration, loaded once and used by every block after it. Re-sending `T` coefficients with every
block would work and would be absurd.

So a firing needs two things that outlive it:

| what | lifetime | written by | read by |
|---|---|---|---|
| `taps` — the `T` coefficients | **held** until replaced | a `LOAD_TAPS` firing | every `FILTER` firing |
| `carry` — the last `T−1` samples | **rewritten every firing** | every `FILTER` firing | the *next* `FILTER` firing |

Two flavours, two lifetimes, one module. That pairing is the reason this example is a filter and not
something smaller — a design with only one of them cannot show that the two are different, and
[`state_toy`](../../guide/memory/hwstate.md) (a running total) is exactly that single-flavour case.

Neither is a channel between components, and neither lives across a bus. Both are storage the module
owns, and both are declared with `add_state` — see [Cross-firing state](./state.md).

## The pipeline

Addresses mean `m_axi`, and a `hls::task` body cannot own an `m_axi` port
([the free-running rule](../../guide/comp_codegen/freerunning.md)). So `fir_block` is a **composite**,
in the same anatomy as `mem_copy` and `interleaver`:

```mermaid
flowchart LR
  cmd([FirCmd]) --> RX[fir_cmd_rx]
  RX -->|"MemRCmd · desc"| MR[MemRStream]
  MR -->|"desc · taps-or-block"| CP[fir_compute]
  CP -->|"MemWCmd · desc · y"| MW[MemWStream]
  MW --> done([done])
  CP -.->|"taps · carry (add_state)"| CP
  gmem0[("m_in · gmem0")] -. read .-> MR
  MW -. write y .-> gmem1[("m_out · gmem1")]
```

Four stages, of which **two are framework components** — `MemRStream` and `MemWStream`, with their
shipped, XSI-verified timing — and two are the design's own:

- **`fir_cmd_rx`** reads one `FirCmd` off the plain host boundary and frames the reader's command
  stream as one read: `[MemRCmd | FirDesc]`, with the descriptor relayed as a header (`fwd_bursts=1`)
  so it arrives welded to its data and can never pair with the wrong burst.
- **`fir_compute`** is the whole custom design: it reads `[FirDesc | data]`, dispatches on the opcode,
  and frames the writer's stream. It is the only stage holding state.

Compared with the interleaver this is **shorter by two stages**, and the reason is worth noticing: the
interleaver needed `il_load` / `il_store` to land `X` in a block RAM, because a gather does *random*
access. A FIR is a **streaming** kernel — it touches each input once, in order — so there is nothing to
buffer and no stream-of-blocks anywhere in this design.

## One leaf, two opcodes

Both commands arrive over the same stream and are handled by the same task body, dispatched on
`FirDesc.op`:

| opcode | the `n` words are | state effect | writes back |
|---|---|---|---|
| `LOAD_TAPS` | coefficients | fills `taps` | **nothing** |
| `FILTER` | a block of samples | reads `taps`, rolls `carry` | `n` output samples |

Dispatch on an enum equality is inside the extractor's vocabulary (the same shape `poly` uses); ordering
comparisons are not. That is why the opcode is an `EnumField` reaching C++ as a real `enum` rather than
a bare integer.

Handling both in one leaf is a deliberate choice. The alternative — a separate tap-loader component —
buys nothing here and costs a second component plus a channel, because of the next point.

## The tap load does *not* overlap the compute

`LOAD_TAPS` and `FILTER` are separate firings of one task, strictly ordered by the command stream.
Job *n*'s coefficients are **not** staged while job *n−1* computes.

That is what keeps the taps *state* rather than a *channel*. The moment two firings overlap, the taps
stop being storage the module owns and become data handed between two components — which means a
[stream-of-blocks](../../guide/interface/sob.md), a second component, and a lock protocol whose
producer/consumer counts have to balance globally. A FIR gains very little from that overlap, and the
analysis (including why a conditionally-acquired lock passes C-sim and hangs in RTL) is written up in
`plans/add_state.md` rather than built.

## The firing that writes nothing

`LOAD_TAPS` consumes a block of coefficients and produces no output data. That sounds harmless. It is
the single most dangerous shape in this design, and it is worth being precise about why.

### The invariant

A free-running composite has **no scheduler**. Each stage is an independent process looping forever —
read inputs, compute, write outputs — and nothing anywhere tracks "jobs". The only thing holding the
pipeline together is stream backpressure: a stage blocks when an input FIFO is empty or an output FIFO
is full. So the whole design rests on one invariant:

> **Every firing must move exactly one token through every stage.**

A no-output opcode is the natural way to break it. Had `fir_compute` simply emitted nothing for
`LOAD_TAPS`, the token counts would stop matching, and that fails in one of two ways:

- **A lost job.** `MemWStream` waits for a command that never comes. When the *next* job's `MemWCmd`
  arrives it serves that one instead — two commands, one completion. Nothing hangs in RTL; the *host*
  hangs, waiting on a completion that will never exist.
- **A true deadlock.** Where a stage's control flow assumes one burst per firing and reads
  unconditionally, it consumes the next job's data as this job's, and the misalignment cascades until
  some stage blocks on a burst that will never arrive.

The recurring shape underneath both is **a zero-count quantity handled as though it were non-zero**.
This codebase has been bitten three times:

| where | the bug | the fix |
|---|---|---|
| free-running pipelines generally | an un-paced N-stage pipe deadlocks at `done = N+1` | a per-job token at every stage |
| `mem_r_stream_framed_task` | a relay read a word when `fwd_bursts == 0` | an `if (nfwd > 0)` guard |
| `MemWStream`'s pysim model | `get(nwords_max=0)` dequeues a whole burst and truncates the *result* — a zero-length write silently ate the next command | skip the drain when `len == 0` |

The third was found *by this example*, and it is the sharpest of the three: the RTL was correct all
along (`for (w = 0; w < 0; …)` trips zero times), so it was **pysim and RTL disagreeing** about a
command the protocol allows.

### The resolution

Not a special case for the load — the opposite. The write side stays **uniform across both opcodes**,
so `LOAD_TAPS` still frames a write command, with `len = 0` and `fwd_bursts = 1`:

- the writer's store loop trips zero times, so no AXI transaction is issued at all;
- its echo loop still forwards the descriptor, so the job lands its completion on `s_done`.

There is no branch anywhere that might or might not emit, which is precisely what makes it safe. The
general rule to carry away: **give every firing the same token path, and make a zero-length quantity
trip its loop zero times rather than once.**

The testbench then asserts the completion *count and order* — never merely that the run finished —
because a wedged pipeline looks exactly like success from the outside.

## Samples, words, and packing

One more convention runs through the whole design. Commands and descriptors count **samples**; the
memory streams move **words**. At `LW = MEM_DW // W` samples per word those are different numbers, and
an `n`-sample block occupies `ceil(n / LW)` words.

Neither side hand-rolls that packing: the kernels use the generated
[`<stem>_array_utils`](../../guide/vectorization/hls/arrayutils.md) lane routines and the Python model
uses `DataArray.serialize`, which are one contract — so the numpy golden and the Vitis kernel agree
bit-for-bit at any `LW`. The packing factor is integer division, so an awkward width is not a problem:
`W = 18` gives `LW = 1`, `W = 16` gives 2, `W = 8` gives 4.

## Where to next

- [Cross-firing state](./state.md) — how `taps` and `carry` are declared, and where they land in the
  generated C++.
- [Fixed point](./fixedpoint.md) — the one format, and the accumulator the format algebra derives.
- [Python](./python.md) — the design in code.
