---
title: Interleaver (gather)
parent: Examples
nav_order: 6
has_children: true
---
# Interleaver — a gather accelerator with a custom compute stage

This example builds on [`mem_copy`](../memcpy/). Where the data mover copies a buffer unchanged,
`interleaver` computes a **gather** — `Y[i] = X[P[i]]`, reordering `X` under an index vector `P` — so it
adds the two things a pure data mover never needs: a real **compute** stage, and **on-chip random
access** (holding `X` in block RAM so the index-driven reads are single-cycle). It is the same
[concurrent free-running flow](../../guide/flows/concurrent.md): a composite of `ap_ctrl_none`
`hls::task`s, wired by internal streams and stream-of-blocks, driven by a command stream and reporting on
a done stream.

`mem_copy` reuses only pre-calibrated *infrastructure* and so calibrates nothing itself. The interleaver
is the counterpart: its `il_compute` gather is the design's **own** kernel, whose timing does not ship —
so this is the example where you **fit a custom component's timing**, the half of the
[calibration story](../../guide/calib/) `mem_copy` has none of. It still *reuses* the shipped
infrastructure — the `m_axi` bus law is loaded from the platform exactly as before — and layers its own
compute model on top.

## Learning Objectives

In going through this example, you will learn to:

- Model a **gather / permutation** accelerator (`Y[i] = X[P[i]]`) as a composite of free-running
  `FreeRunComp` stages — load, a custom **compute**, and store
- Use a [**stream of blocks**](../../guide/concurrency/python/sob.md) to give the compute stage
  **random access** to a buffer (`X[P[i]]`), and overlap the next job's load with this job's compute
- **Reuse the framework `MemRStream` / `MemWStream`** as the read/write stages — framing two reads (P and
  X) with an **in-band descriptor**, which also paces the free-running pipeline (one job in flight, no
  deadlock) and returns a commit-timed done, with no custom mem adaptors and no separate token
- **Visualize** the six-stage pipeline overlap on an [activity diagram](../../guide/timing/activity.md),
  straight from the loosely-timed simulation
- Reuse the platform's shipped **infra timing** — the mem-stream adaptors *and* the `m_axi` bus law —
  then **fit the custom compute stage's own loop model** from a size sweep (the
  [direct method](../../guide/calib/fit.md)) and store it in the platform library so a build loads it
  with no re-fit

## In this example

The pages build the design up from Python, parallel to [`mem_copy`](../memcpy/):

1. [Module overview](./interleaver.md) — the gather, the six stages, the stream-of-blocks for random
   access, and the per-job token forwarding.

_The remaining pages — the Python model, the testbench, the DUT and testbench codegen, the RTL run, the
activity-diagram visualization, and fitting the custom compute stage — are being written alongside the
calibration work; this section grows as they land._
