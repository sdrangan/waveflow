---
title: "Simulation speed: the SSR FFT"
parent: Simulation speed
nav_order: 2
summary: "One bit-exact 1024-point FFT run three ways on the same frames: the untimed Python model in vectorized numpy (1.87 ms a frame), Waveflow's pysim (2.29 ms flat, 4.44 ms with one process per task) and the synthesized RTL under XSI (131 ms, plus about two and a half minutes of synthesis and build per design point). What the pysim adds and why, how its cost came down from 350 ms a frame in one day, the lessons for the next block, and an estimate against a compiled model, which the pysim would call too: about 0.05 ms a frame untimed, about 0.47 ms in the flat pysim, the timing then most of the cost."
---

# Simulation speed: the SSR FFT

## The set-up

The [SSR FFT example](../../examples/ssr_fft/index.md), unchanged: `SsrFft` at `L = 1024`, four
samples a cycle, `ap_fixed<16, 2>` input, the ping-pong reorder, one `RadixWord` port each way, driven
by its [testbench](../../examples/ssr_fft/test.md) -- a driver, the FFT, a sink -- with frames back to
back. The same random frames, and the same bit-exact golden, for all three:

```text
untimed:  frames --> model.pipeline (the bit-exact arithmetic, one frame at a time) --> bins
pysim:    StreamDriver --> SsrFft (16 tasks, or flat) --> TimedSink          (SimPy, timed)
RTL:      AxisMaster BFM --> synthesized ssr_fft (16 hls::tasks) --> AxisSlave BFM   (XSI)
```

- **Untimed** is `model.pipeline`: the Python a system engineer would evaluate the algorithm with,
  and the *same functions* the pysim's tasks call. The comparison is therefore model to model. It is
  **vectorized numpy**: each stage is a handful of whole-array operations over the frame, with no
  Python loop over samples -- the way models like this are normally written, and a reasonable
  baseline. It is not compiled; what that would change is estimated
  [below](#with-a-compiled-model-an-estimate-not-yet-measured).
- **The pysim** comes in two granularities. `pysim="tasks"`, the default, runs one SimPy process
  per `hls::task` -- 16 of them -- with every internal edge a timed stream. `pysim="flat"` runs one
  process for the whole FFT with the same frames at the same cycles (see
  [the guide](../../guide/dsp/ssr_fft/timing.md#the-flat-pysim-one-process-for-the-fft)).
- **The RTL** is the csynth'd design simulated by XSI through the system flow: untraced, the
  testbench's BFMs timing every word.

```bash
python -m examples.ssr_fft.ssr_fft_runtime --rtl    # -> examples/ssr_fft/runtime.json
```

## Results

Per frame, fitted over 10, 50 and 100 frames; every run bit-exact:

| | per frame | vs untimed | one-time cost |
|---|---|---|---|
| untimed model (vectorized numpy) | **1.87 ms** | 1x | none |
| pysim, flat | **2.29 ms** | 1.22x | ~15 ms to build the graph |
| pysim, one process per task | 4.44 ms | 2.4x | ~14 ms |
| RTL under XSI | **131 ms** | 70x | XSI build 24.5 s (compile 2.4, elaborate 17.1, testbench 4.9); csynth ~131 s |

Measured 2026-10-13, except csynth, measured the same morning on the same machine (it does not
depend on the number of frames). The machine is described on [Simulation speed](index.md#results).

### With a compiled model (an estimate, not yet measured)

The untimed model above is vectorized numpy, which is how most system engineers write such a model.
A compiled one would be faster still, but it is real work: the arithmetic rewritten as typed loops over
integers -- each fixed-point step a multiply, a rounding shift and a wrap -- a build step for Cython
(or a Numba version that supports the Python in use), and the result proven bit-exact all over again.
Done, a bit-exact radix-4 FFT should take tens of nanoseconds a sample. The pysim calls the same
arithmetic, so it gets the same saving and keeps its own overhead:

| | per frame (estimate) | vs compiled untimed |
|---|---|---|
| untimed model, compiled | ~0.05 ms | 1x |
| pysim, flat, calling the compiled model | ~0.05 + 0.42 = ~0.47 ms | ~9x |
| pysim, one process per task, calling it per stage | ~0.05 + 2.57 = ~2.6 ms | ~50x |
| RTL under XSI | 131 ms | ~2600x |

The 0.05 ms is an estimate (20-50 us is the plausible range); the overheads, 0.42 and 2.57 ms, are
the measured differences between the pysims and the untimed model above, and do not depend on how
the bits are computed. The flat pysim already runs the whole chain in one place, so a compiled
kernel for the full pipeline would replace that loop as one call; the per-task pysim would need the
kernel split per stage. Against a compiled model, the timing is no longer a small addition -- it
is most of the cost -- which is why [the cost per event](index.md#what-is-still-slow) is the next
thing to reduce.

## Insights

- **The flat pysim adds 0.42 ms a frame to the arithmetic** -- about a dozen SimPy events. For this
  block, the timing of the hardware costs 22% on top of the bit-exact model.
- **The per-task pysim adds 2.6 ms a frame**: each of the 16 tasks unpacks its input words, repacks
  its output and crosses a timed stream, every frame. What it buys is visibility inside: each task's
  firings, each internal edge's occupancy. Use it to look inside; use the flat one to run long.
- **The RTL is about 57 times slower than the flat pysim per frame, and costs minutes before the
  first frame.** A design change pays csynth and an XSI build; the pysim pays nothing. For
  design-space exploration over designs, rather than workloads, that is the larger gap.
- **The two agree.** The pysim's first frame is within 5 cycles of the RTL's at `L = 16 .. 1024`,
  and its interval is exact ([timing](../../guide/dsp/ssr_fft/timing.md)). The untimed model has no
  timing to compare.
- **The untimed baseline is vectorized, not compiled**, and a reviewer may ask how much faster it
  could be. Vectorized numpy is a fair baseline -- it is what people use -- but this model also
  reproduces the vendor library operation by operation: a radix-4 butterfly is 16 general complex
  multiplies by constants that are exactly `1, -j, -1, +j`, each with three quantization points.
  That is about a thousand int64 operations per sample and about 3000 numpy calls per frame. A
  compiled bit-exact model would be far faster -- an estimate is in
  [the table above](#with-a-compiled-model-an-estimate-not-yet-measured). Because the pysim calls
  the same functions, it would get the same saving: the additive overhead is what this page measures.
- **An untimed model can also batch.** Processing a whole capture in one call amortizes every
  per-frame cost; a timed simulation steps frame by frame, because the timing is per frame. That is a
  real cost of timing, not an implementation detail.

## How it got here

The per-task pysim at `L = 1024`, the same day:

| step | per frame |
|---|---|
| start | ~350 ms |
| array encoding vectorized: frames packed to words with numpy, not element by element | ~15 ms |
| wide words straight to 64-bit chunks; schema caches; no process or resource event per transfer | 4.4 ms |
| `pysim="flat"`: one process, same frames at the same cycles | 2.3 ms |

(The first two were measured within longer runs on the same machine; the last two by this page's
script.)

## Lessons learned

For the next block whose pysim is slow:

1. **Profile before guessing.** The 350 ms was neither SimPy nor the arithmetic: 99% of it was
   converting words to samples one element at a time in the schema layer. The arithmetic was 0.05 s
   of 8 s.
2. **Move data as arrays.** Give a `DataArray` an `ndarray` value and its words are packed with
   numpy, wide words (over 64 bits) as 64-bit chunks; a new element type joins by defining
   `_flat_bits_numpy` / `_from_flat_bits_numpy`. Never pack or unpack per element in a pysim body.
3. **Count events per transaction**, not just time. Spawned processes, and resource requests that
   could not wait anyway, were most of them; the framework now avoids both. What is left should be
   the modelled delays and real waits.
4. **Choose the granularity on purpose.** One process per hardware task is faithful, and costs an
   unpack, a pack and a hop per task per frame. When the chain never stalls, the latencies add, and
   one process writing at the summed offset is exact -- prove it with an equality test against the
   per-task model, bits and cycles, as `test_flat_pysim_is_the_task_chain` does. When a stage is
   slower than its input (the stream-of-blocks reorder), frames queue, and the flat model does not
   apply.
5. **Measure honestly.** Background load swung wall times two to three times on the test machine:
   compare versions by alternating fresh processes and keeping each one's best run, and require the
   simulated outcome -- every arrival time, every firing -- to be identical, not just the bits.
6. **Check the other backends after a codec change.** The same array encoding writes the XSI
   scenarios and reads the RTL captures; the XSI gates had to pass too.
7. **On-time wrong answers look like success.** While measuring, the RTL at `L >= 256` produced every
   frame on schedule and every frame wrong -- its twiddle ROMs' data files never reached the
   simulator, and undriven bits read as ones. Only a bit check per frame showed it; the BFMs now
   stop the run on undriven data.

## Future work

- **A compiled bit-exact model**, to replace the estimate above with a measurement: typed integer
  loops compiled with Cython (or Numba), the ±1/±j butterfly products as sign changes and swaps, the
  quantization steps fused; checked against the golden, then timed untimed and inside the flat pysim.
  It lowers the untimed baseline and the pysim alike, and tests the additive model directly.
- **Other lengths and widths** (`L = 16 .. 4096`), and the stream-of-blocks reorder, which has no
  flat model.
- **Partitioned simulation.** SimPy runs on one core. A system cut at a link with long latency (two
  UEs and their wireless channel, synchronized once a TTI) could run each side in its own process --
  a direction, not a feature.
