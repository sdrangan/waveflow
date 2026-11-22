---
title: "Agent skills: shared kernels"
parent: Agent skills
nav_order: 7
summary: "Can an agent design an accelerator in which one expensive kernel is shared by several clients, with a feedback loop and data-dependent control -- and how long does it take? A held-out design problem (one FFT shared by independent clients, a coarse search then an iterative refinement of data-dependent length), run before and after the shared-kernel work of plans/shared_kernel.md. Before: agents with and without Waveflow reached the right architecture on paper; the Waveflow agent hand-wrote its mux and called a cycle in a composite untested. After: four chat runs (Opus, Sonnet) all used StreamArbiter, StreamDemux and SsrFft's per-frame direction correctly; one build run went from an empty folder to 11 passing pysim tests in 14 minutes, and found four framework gaps, since fixed. None of the runs considered replicating the FFT."
---

# Agent skills: shared kernels

> **Status: one before-and-after study, 2026-11-21 to 22.** Chat runs and one build run, scored by one
> reviewer. The build run stops at pysim: nothing it wrote has been through csynth or RTL. The held-out
> problem's specification is kept outside the repository, so that `waveflow kb` and a grep cannot find it.

## The question

The other skill tests ask whether a skill makes an agent follow a pattern it would otherwise miss. This one
asks a larger question: **given a design whose hard part is sharing one expensive kernel among several
clients, can an agent design and build it in Waveflow -- and how long does it take?** A shared kernel is
where agents working directly in Vitis HLS struggle: reusing one unit from several places needs
free-running `hls::task`s, an arbiter, tags, credits and a feedback loop, and verifying it needs an RTL
testbench for a free-running top that plain cosimulation does not give.

## The held-out problem

A multi-client estimator. Each client (an antenna) sends independent jobs. A job runs a coarse grid
search, where each grid point needs a forward and an inverse transform, then an iterative refinement with
a line search, which stops at a tolerance or an iteration limit, so jobs end at times that depend on the
data. Every transform in every phase, for every client, must go through **one** FFT, kept busy across
clients. The full specification, the scoring rubric and every run's report are outside the repository.

The scoring rubric has seven points:
1. the maths;
2. the blocks used;
3. the return-path rules (end-to-end credit, feedback first, a feedback frame announced only once
   complete, store and forward that does not halve throughput, the unit never blocking on a result);
4. a rate check from the FFT's measured latency and interval;
5. extensions named where Waveflow lacks something;
6. a verification plan with counts asserted;
7. for a build run, how far the design gets.

## Before: 2026-11-21

Three fresh agents answered the problem as a chat reply (Opus 5.5, one run each):

| arm | time | what it proposed |
|---|---|---|
| Vitis HLS only, from its own knowledge | 6 min | five free-running `hls::task`s around one `hls::fft` with a run-time direction, a tag in a side FIFO, credits, a polling scheduler, and a stated deadlock argument. Flaws: the feedback frame announced before its data (the arbiter commits early and starves the FFT); one client's late samples block every other client; verification reduced to "csim, then cosim" |
| Waveflow, with the skills of the day | 5 min | the same architecture, better grounded (`SsrFft`'s measured latency, a conj trick for its fixed direction, the feedback frame requantized). But it hand-wrote the mux, found no arbiter, called a cycle inside a composite "untested territory", proposed no extension, and had the same early-announce flaw |
| Waveflow, after the rule change of Stage 0 | 3.5 min | one feeder as the FFT's only writer (no arbiter needed), other clients' frames scheduled between a forward pass and its inverse, which avoids the stall; it trusted a stale guide sentence saying the FFT's inverse was not built |

So both agents knew the pattern. What the Waveflow agent lacked was the infrastructure around it: blocks,
rules for a cycle, and the permission to extend Waveflow.

## What was built in between

`plans/shared_kernel.md`, Stages 0-6. Agents wrote it on three machines: this planning session in the
cloud, a laptop with Vitis, and a lab machine with the RFSoC licence. They coordinated through git and
messages. It took about 8 hours of wall-clock time from the plan to Stage 6, including the RTL runs:

| stage | what |
|---|---|
| 0 | the rule: never work around a gap; propose and build the extension |
| 1 | a stub composite with a cycle, through pysim, codegen and RTL (`examples/ring_stub`) |
| 2 | `StreamArbiter`, `StreamDemux`, and `wait_any` for a polling stage's model |
| 3 | `SsrFft(direction="runtime")`: the direction chosen per frame |
| 4 | the concept page, [Sharing a kernel](../../guide/systems/shared_kernels.md) |
| 5 | the curated example, an overlap-save filter bank on one shared FFT ([os_filterbank](../../examples/os_filterbank/index.md)) |
| 6 | the `waveflow-shared-kernel` skill and the `stream_shared` frame; its trigger check scored 8 of 8 |

## After: 2026-11-22

Four chat runs, with the verbatim problem and the same instructions as before, at Stage 6 (ead8011):

| run | time | tool calls | outcome |
|---|---|---|---|
| Opus 5.5, 1 | 4.0 min | 12 | `os_filterbank`'s shape (below); the maths right, including the extra transform the second parameter's gradient needs; that gradient frame sent speculatively with each trial |
| Opus 5.5, 2 | 4.9 min | 11 | the same shape, the most explicit on store and forward |
| Sonnet 5.5, 1 | 2.6 min | 13 | the same shape |
| Sonnet 5.5, 2 | 2.4 min | 9 | the same shape, with a splitter after a round-robin ingest arbiter |

`os_filterbank`'s shape, which all four runs reached:
- per-client fronts, each holding a credit;
- a `StreamArbiter` in priority with the feedback on input 0;
- one `SsrFft(direction="runtime")`;
- each client's data resident beside the FFT;
- a controller with one state machine per client, polling with `wait_any`;
- `StreamDemux` by header to a port per client, so RTL and pysim recordings stay comparable.

None hand-wrote a mux, used the conj trick, or announced a feedback frame before its data.

**One build run** (Opus 5.5) was given an empty folder, the problem, n = 64 and four clients. It was to
build as far as pysim goes. It took **14 minutes and 62 tool calls**:
- 9 minutes reading the guide, the skills and `os_filterbank`;
- the 600-line design written in one go;
- the first tests running a minute later;
- all **11 tests passing** at 13.5 minutes.

The 11 tests cover:
- the gradient checked as the true derivative;
- the golden reaching the floating-point maximum within 2e-3 rad;
- each stage alone against the golden;
- the composite with four clients and seven jobs arriving at different times and finishing out of order,
  every count and `tx_id` asserted, and all 308 frames through the one FFT;
- a deadlock witness that returns cleanly;
- a check that the generated top has the loop and a single FFT.

The FFT was 85% busy while all four clients were in the loop. It did not write HLS bodies for two of its
three stages, and nothing went to csynth or RTL.

**What no run did:** none considered replicating the FFT. The skill's checklist asks for it, and the
[filter bank's numbers](../../examples/os_filterbank/index.md) show sharing winning by a wide margin, but
no agent stated the trade.

## What the build run found in Waveflow

These four gaps were fixed the same day (`plans/shared_kernel.md`, *The Stage 7 fixes*):

1. **pysim could pass a loop that deadlocks at RTL.** A blocking write of a burst larger than its link's
   depth is admitted whole once the link is empty; RTL stalls the writer word by word. Each composite now
   checks its cycles after a run and warns (`LoopDepthWarning`).
2. **Nothing could route by frame after `SsrFft`**, which frames its output by count alone, so the
   agent's post stage became the bottleneck. The new [`StreamFramer`](../../guide/streams/framer.md) adds
   TLAST.
3. **An input's first burst could not arrive late** in a harness. `StreamBurst(gap_before=n)` now states
   when a burst arrives, in both the pysim driver and its RTL twin.
4. **A nested schema's header could be silently missing** until csynth. `DataSchemaStep` now warns.

The same day, the lab machine's resource sweep found and fixed an RTL hang in `StreamArbiter` from four
clients. Every Stage 2 test had used three.

## What it shows

- **Agents already know the pattern.** Before any of this work, both arms proposed an arbiter, tags,
  credits and a feedback frame. The difference Waveflow makes is everything around the pattern: tested
  blocks, rules for a cycle, a reference design, and verification that cannot pass on a deadlock.
- **With that in place, a correct design is fast.** The chat runs took 2.5 to 5 minutes; a design that
  simulates, against a golden, with counts asserted, took 14.
- **The toolchain knowledge was the expensive part, and it is reusable.** The arbiter took a few hours to
  write and verify. That was not for its code: it went on the traps only RTL reveals, two loop shapes
  that hang, and a hang that needs four inputs. The next design gets the block with those found.
- **The evaluation found real bugs.** A build run is a better test of a framework than a chat reply: in
  14 minutes it hit four gaps that the stages' own tests had missed.

## Limits

- **Small samples:** two chat runs per model and one build run.
- **Adaptation more than invention:** the chat runs adapted `os_filterbank` closely, which is what a
  reference design is for. A problem with no close example would test invention.
- **pysim only for the build run;** its RTL behaviour is untested.
- **One reviewer.**
- **The same agent family throughout:** Claude Code, Opus 5.5 and Sonnet 5.5.
- **The held-out problem has now been seen** by the agents that worked on the framework. It stays usable
  only while its specification stays out of the repository, and a fresh test needs a fresh problem.
  This page describes it only in outline and is outside the `waveflow kb` index, but a grep finds it: a
  later run should be told to stay out of `plans/` and `docs/evaluation/`, as these were told to stay out
  of `plans/`.
