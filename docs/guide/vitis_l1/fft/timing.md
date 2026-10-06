---
title: Latency and II for a vendor block
parent: The Vitis FFT
grand_parent: Vitis L1 Blocks
nav_order: 5
audience: python
api: [VitisFft, cycles_seed, timed_delay, call_after, TimingModel, LookupCalibModel]
summary: "A bit-exact model predicts what comes out, not when. This page covers why a vendor FFT needs two timing numbers rather than one (latency and initiation interval), why a single sequential run_iter cannot express both, how VitisFft models it with run_iter plus a deferred write (SimObj.call_after) bounded by in-flight slots, and what the synthesized hardware actually measured — 45 and 46 cycles at L=16, meaning no frame overlap, against a plan estimate that would have promised six frames in flight. Also why C-synthesis cannot supply these numbers at all."
---

# Latency and II for a vendor block

The models in `waveflow/vitis_l1/` reproduce the vendor's **arithmetic**, deliberately and by
design. They say nothing about parallelism, which is correct for bit-exactness and insufficient for
an `HwModule`: a module in a design also has to predict *when* data appears.

## One number is the wrong shape

A pipeline exists precisely to decouple two things:

* **latency** — a frame's whole residence, first input word to last output word. That is what
  C/RTL co-simulation reports per transaction (`ap_start` → `ap_done`), so a measured number goes in
  unchanged;
* **initiation interval (II)** — how often a new frame can *start*.

A model carrying a single "the transform costs T" is wrong the moment the block sits in a chain
with anything else, which is the only reason to build it. Carrying both is right for every
feed-forward use, and needs no per-stage machinery.

**A single sequential `run_iter` cannot express both.** Its loop is read-frame → delay →
write-frame, so frame *k+1* is not accepted until frame *k* has been written: back-to-back jobs
serialize and II collapses into latency. That is wrong exactly where it matters, with a host
issuing frames back to back.

So `VitisFft` splits a firing in two. `run_iter` reads a frame, computes the bits **once** (in
zero simulated time), and defers the write with `SimObj.call_after`; then it returns to accept the
next frame while this one is still in flight:

```python
def run_iter(self):
    yield self._slots.get(1)                      # blocks once max_inflight frames are inside
    x = yield from self._read_lanes(per_lane)     # L/R cycles
    y = self._transform(x)                        # bits, zero time
    self.call_after(t_write - self.now, self.store, y, slot=self._slots)
    yield self.timeout(t_next - self.now)         # II, measured from the frame's first word

def store(self, y):
    yield from self._write_lanes(y)               # L/R cycles; the slot is released after
```

`t_write` is the frame's first input word plus `latency − L/R`, so the last output word lands
exactly `latency` after the first input word; `t_next` is the first input word plus `II`.

`store` is a second entry point into the module, so it is held to `call_after`'s contract: a pure
function of its arguments, touching only the output ports and never state `run_iter` reads. The FFT
carries nothing from one frame to the next, so it meets that trivially. A block whose output side
updated state its input side reads would not, and should be two modules instead.

Keeping intake as `run_iter` matters: `_run_iter_forever` is what populates `firing_records` and
drives `timed_delay`, so the calibration path keeps working unchanged. Two pysim processes against
one C++ task is not a divergence — `kernel_task()` is the realization hook and the generator never
extracts `run_iter`, so the pysim's process structure is free. The two processes are the pysim
expressing what Vitis implements with `#pragma HLS DATAFLOW` inside a single call.

**The slot count is the third number, and it is not cosmetic.** `max_inflight` bounds frames in
flight — by default `ceil(latency / II)` — and is what makes back-pressure correct when the
consumer stalls: a frame holds its slot from before it is read until its last word is written.
Unbounded, the module would accept frames forever while its consumer is blocked, which no hardware
does. It is also the *paced* form of a free-running chain, which keeps the design clear of the
recorded un-paced deadlock.

## The numbers have no defaults, and that is the point

`latency_cycles` and `ii_cycles` are one pair: either alone is a plausible-looking wrong model, so
neither is accepted alone. Leave both unset and the module is untimed — bits only.

A number nobody measured is worse than no number, because people believe it. `cycles_seed()`
offers the plan's `II ≈ L/R` estimate, labelled a seed, and deliberately offers **no** latency.

## What the hardware measured

From C/RTL co-simulation of the `ap_ctrl_hs` top in `verifyHwModule/` at `L=16` (Vitis 2025.1,
`xc7z020clg484-1`, 10 ns), recorded in `tests/vitis_l1/fft/verifyHwModule/results/cosim_cycles.json`:

| | latency (min) | interval (min) |
|---|---|---|
| the `ap_ctrl_hs` top | **45** | **46** |
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

**The pysim reproduces it.** Given the measured 45 and 46, `VitisFft` finishes the same four
back-to-back frames at cycle 183, against cosim's 184 (which counts to the `ap_done` after the last
word). `test_cosim_four_frames_back_to_back` in `tests/vitis_l1/fft/test_hwmodule.py` holds it to
within one cycle.

The same mistake is easy to make in the pysim, and was made here: with the `R` lanes transferred
one after another instead of concurrently, a frame costs `L` cycles instead of `L/R`, and a
declared II of 8 reads as 16. Both the module and its test harness move all lanes concurrently for
that reason. **A serialized testbench measures itself, not the module** — worth knowing before
trusting any II out of a simulation.

## The free-running top, at RTL

The design top that `composite_top_spec` generates is different: `ap_ctrl_none`, the body running as
an `hls::task`. Vitis cannot co-simulate it, so `examples/vitis_fft` drives it under XSI and reads the
ports (`TVALID && TREADY`) off the waveform. On the RFSoC 4x2 at 250 MHz:

| L | interval (back to back) | latency, isolated frame: mean (range) |
|---|---|---|
| 16 | 41 | 43 (43 – 43) |
| 64 | 120 | 133 (121 – 139) |
| 256 | 480 | 519 (454 – 548) |

- **The interval is about the latency -- as `VitisFft` connects the core.** Its body calls
  `fft<>`, the vendor guide's *non-streaming connection*, which serializes frames; the vendor's bare
  array-port core does the same (1477 / 1478 at `L = 1024`). The *streaming connection* (`innerFFT`
  in a DATAFLOW region) overlaps frames, but the library's per-call commutators still floor the
  interval near 2.5 `L/R` -- see [why](index.md#why-vitisfft-is-far-below-the-architectures-rate).
  `waveflow.dsp.ssr_fft.SsrFft`, the same arithmetic as free-running tasks, reaches `L/R`.
- **Latency depends on arrival phase.** The core's input transposer runs a commutator on a
  free-running internal cycle; an isolated frame out of step with it waits up to a period inside the
  transposer. Back to back, frames stay in step and the interval is exact. The first frame after reset
  is in step by construction and is the fastest (391 at `L = 256`).
- **The input runs ahead.** The body's input lanes take about two frames before the core is free.
  `VitisFft` paces intake by the II, which reproduces the output timing and holds an upstream producer
  back slightly longer than the hardware would.

So the module's timing is **calibrated on a platform, with a stated error**.  `VitisFft` is framework
infrastructure, so its two delays are `TimingModel`s stored on
`waveflow/calib/platforms/rfsoc4x2_bfm_250mhz` (part and clock), reloaded by any design on it:

* **proc** -- last input word in to last output word out, the delay `run_iter` defers the write by;
  the **mean** over a sweep of arrival phases, because an LT model cannot know the phase (the mean
  minimizes the squared error), with the measured spread as its error;
* **ii** -- the back-to-back interval, exact.

Both are **added** to the channels' own transfer cost, which the residual fit subtracts out, so the
module never restates a channel's timing.  Each is a **lookup per length**: the law
`b0 + b1 L + b2 L log2 L` fits L = 16, 64, 256 exactly but predicts L = 1024 14-20% low, because Vitis
changes the implementation between them.  An unmeasured length is refused, not extrapolated.  The
fixture `waveflow/calib/fixtures/vitis_fft.py` measures, collects and refits to a fixed point; at
convergence the pysim reproduces the RTL's mean proc span and interval at every calibrated length.
See [A vendor FFT, frames in and out](../../../examples/vitis_fft/index.md).

## C-synthesis cannot supply these

The obvious source would be `csynthparse`, which pulls `PipelineII` and `Latency` per module out of
`csynth.xml`. For this top every one of those fields reads `undef`: the top is a `DATAFLOW` region,
so Vitis does not bound it statically. Co-simulation is the only source, which makes the RTL rung
load-bearing rather than merely confirmatory.

## What is still open

A design that needs the FFT's nominal rate sustained needs a different construction: parallel
instances (AMD's own approach), or an FFT written as a flat chain of free-running Waveflow stage
modules -- the bit-exact model is already organised stage by stage. The input buffering the RTL shows
is not modelled; it would matter only for a producer whose timing depends on when the FFT releases it.
