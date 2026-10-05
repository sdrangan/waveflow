---
title: Latency and II for a vendor block
parent: Vitis L1 Blocks
nav_order: 3
audience: python
api: [VitisFft, cycles_seed, timed_delay, call_after]
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
an `hls::task`. Vitis cannot co-simulate it, so `examples/vitis_fft` drives it under XSI, four frames
back to back, and the numbers are read off the waveform as `TVALID && TREADY`:

| | latency | interval |
|---|---|---|
| the free-running top (XSI) | 44 | 42 |

- **No adapter cost.** The interval equals the bare array-port core's 42: the 4 cycles above were
  the `ap_ctrl_hs` top's per-call handshake, not the pumps.
- **Still no overlap.** Frames leave every 42 cycles with a 44-cycle residence. The core processes one
  frame at a time; `II ≈ L/R` remains a property of some other construction, not of this one.
- **The input runs ahead.** The body's input lanes and their FIFOs take about two frames before the
  core is free, so the hardware applies its interval at the core and the output, not at intake.
  `VitisFft` paces intake by the II, which reproduces the output timing exactly and holds an
  upstream producer back slightly longer than the hardware would.

Configured with `latency_cycles=44, ii_cycles=42`, the pysim puts every one of the four frames on the
RTL's cycle, up to a constant one-cycle start offset; the gate asserts exactly that. See
[A vendor FFT, frames in and out](../../examples/vitis_fft/index.md).

## C-synthesis cannot supply these

The obvious source would be `csynthparse`, which pulls `PipelineII` and `Latency` per module out of
`csynth.xml`. For this top every one of those fields reads `undef`: the top is a `DATAFLOW` region,
so Vitis does not bound it statically. Co-simulation is the only source, which makes the RTL rung
load-bearing rather than merely confirmatory.

## What is still open

The numbers above are for `L=16`. The free-running top does not overlap frames either, so a
design that needs `II ≈ L/R` needs a different construction — AMD's own wide-stream
`fftStreamingKernel` is the one to try — and its own measurement. The input buffering the RTL shows is
not modelled; it would matter only for a producer whose timing depends on when the FFT releases it.
