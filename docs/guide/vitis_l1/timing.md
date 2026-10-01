---
title: Latency and II for a vendor block
parent: Vitis L1 Blocks
nav_order: 3
audience: python
api: [VitisFft, cycles_seed, timed_delay, transaction_queue]
summary: "A bit-exact model predicts what comes out, not when. This page covers why a vendor FFT needs two timing numbers rather than one (latency and initiation interval), why a single sequential run_iter cannot express both, how VitisFft models it with two processes and a bounded queue, and what the synthesized hardware actually measured — 45 and 46 cycles at L=16, meaning no frame overlap, against a plan estimate that would have promised six frames in flight. Also why C-synthesis cannot supply these numbers at all."
---

# Latency and II for a vendor block

The models in `waveflow/vitis_l1/` reproduce the vendor's **arithmetic**, deliberately and by
design. They say nothing about parallelism, which is correct for bit-exactness and insufficient for
an `HwModule`: a module in a design also has to predict *when* data appears.

## One number is the wrong shape

A pipeline exists precisely to decouple two things:

* **latency** — first input word to first output word;
* **initiation interval (II)** — how often a new frame can *start*.

A model carrying a single "the transform costs T" is wrong the moment the block sits in a chain
with anything else, which is the only reason to build it. Carrying both is right for every
feed-forward use, and needs no per-stage machinery.

**A single sequential `run_iter` cannot express both.** Its loop is read-frame → delay →
write-frame, so frame *k+1* is not accepted until frame *k* has been written: back-to-back jobs
serialize and II collapses into latency. That is wrong exactly where it matters, with a host
issuing frames back to back.

So `VitisFft` uses **two processes and a bounded queue**, following `Rfdc.run_proc`:

| | | records |
|---|---|---|
| **intake** — stays `run_iter` | accept a frame, compute the bits **once**, push `(frame, t_ready)` | the II firing |
| **emit** — its own process | pop, wait until `t_ready`, write the frame out | the latency |

Keeping intake as `run_iter` matters: `_run_iter_forever` is what populates `firing_records` and
drives `timed_delay`, so the calibration path keeps working unchanged. Two pysim processes against
one C++ task is not a divergence — `kernel_task()` is the realization hook and the generator never
extracts `run_iter`, so the pysim's process structure is free. The two processes are the pysim
expressing what Vitis implements with `#pragma HLS DATAFLOW` inside a single call.

**The queue capacity is the third number, and it is not cosmetic.** It bounds frames in flight —
roughly `ceil(latency / II)` — and is what makes back-pressure correct when the consumer stalls.
Unbounded, the module would accept frames forever while its consumer is blocked, which no hardware
does. It is also the *paced* form of a free-running chain, which keeps the design clear of the
recorded un-paced deadlock.

## The numbers have no defaults, and that is the point

`latency_cycles` and `ii_cycles` are one pair: either alone is a plausible-looking wrong model, so
neither is accepted alone. Leave both unset and the module is untimed — bits only.

A number nobody measured is worse than no number, because people believe it. `cycles_seed()`
offers the plan's `II ≈ L/R` estimate, labelled a seed, and deliberately offers **no** latency.

## What the hardware measured

From C/RTL co-simulation of the generated top at `L=16` (Vitis 2025.1, `xc7z020clg484-1`, 10 ns),
recorded in `tests/vitis_l1/fft/verifyHwModule/results/cosim_cycles.json`:

| | latency (min) | interval (min) |
|---|---|---|
| the generated top | **45** | **46** |
| the array-port DUT in `verifyFFT16` | 41 | 42 |

Two things follow, and both are measurements rather than estimates.

**The adapter costs 4 cycles** of each, against the array-port DUT — exactly the `L/R = 4` words
each pump process moves. That is the price of the `R`-wide port group, and it is explainable rather
than mysterious.

**Frames do not overlap.** The interval is *larger* than the latency, so a frame only starts once
the previous one has finished. `plans/vitis_l1_hwmodule.md` seeds II at `N/R`, which is 4 here.
Configured with the measured numbers, `VitisFft` reports `ceil(45/46) = 1` frame in flight, which
is what the hardware does; configured with the plan's seed it would have reported six, promising
six times the throughput the block delivers.

That conclusion was checked rather than assumed. The first measurement used a testbench that
filled the inputs, called the top, drained the outputs, then repeated — a shape that *cannot*
overlap frames whatever the hardware could do, so it could not tell "the design serializes" from
"the testbench never asked it to". A second testbench
(`verifyHwModule/src/tb_pipelined.cpp`) queues every frame's input up front and calls the top back
to back with nothing drained between, and reports the **same** 45 and 46. The serialization is in
the design as built, not in the measurement.

The same mistake is easy to make in the pysim, and was made here: with the `R` lanes transferred
one after another instead of concurrently, a frame costs `L` cycles instead of `L/R`, and a
declared II of 8 reads as 16. Both the module and its test harness move all lanes concurrently for
that reason. **A serialized testbench measures itself, not the module** — worth knowing before
trusting any II out of a simulation.

## C-synthesis cannot supply these

The obvious source would be `csynthparse`, which pulls `PipelineII` and `Latency` per module out of
`csynth.xml`. For this top every one of those fields reads `undef`: the top is a `DATAFLOW` region,
so Vitis does not bound it statically. Co-simulation is the only source, which makes the RTL rung
load-bearing rather than merely confirmatory.

## What is still open

The numbers above are for this top as built, at `L=16`. Whether a different construction overlaps
frames — a free-running `hls::task` body, or AMD's own wide-stream `fftStreamingKernel` — is
untested, and would be the thing to try if a design needs the throughput. An XSI/BFM gate with an
asserted cycle count is also open; these cycles come from Vitis co-simulation.
