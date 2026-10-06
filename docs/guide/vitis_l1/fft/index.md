---
title: The Vitis FFT
parent: Vitis L1 Blocks
nav_order: 1
has_children: true
audience: python
api: [VitisFft]
summary: "AMD's Vitis L1 SSR (super-sample-rate) FFT as the Waveflow module VitisFft. What L and R mean; the architecture inside the vendor core -- the input transposer and its commutators, the radix-4 stages with their inter-stage twiddle rotations, the digit-reversal reorder; why VitisFft runs at a tenth of the rate the architecture allows (the fft<> wrapper, per-call commutators) and what reaches it; why an isolated frame's latency depends on when it arrives; where the arithmetic loses precision and how the output width grows; and what Waveflow adds around it."
---

# The Vitis FFT

`VitisFft` (`waveflow.vitis_l1.hw`) wraps AMD's **SSR FFT** from the Vitis DSP Library
(`xf::dsp::fft::fft<>`): a fixed-point, forward, natural-order FFT of length `L` that takes `R`
complex samples per clock cycle.

- **`L`** is the transform length, fixed at build time (`ssr_fft_param_struct::N`); there is no
  runtime length. It must be a power of `R` (16, 64, 256, 1024, 4096, ...).
- **`R`** is the **super-sample rate**: the number of parallel lanes in and out. In this library it
  is also the butterfly radix -- the two are tied -- and the module supports `R = 4`.

Sample `n` of a frame travels on lane `n % R` at word `n // R`, so a frame enters in `L/R` cycles.

## Inside the core

```mermaid
flowchart LR
  in(["lanes in<br/>R samples / cycle"]) --> cast[cast]
  cast --> swap["input transposer<br/>(swap: a chain of commutators)"]
  swap --> s1[stage 1]
  s1 --> s2[stage 2]
  s2 -.-> sS[stage S]
  sS --> dr["digit-reversal<br/>reorder"]
  dr --> out(["lanes out<br/>R samples / cycle"])
  stg["each stage:<br/>radix-4 butterflies<br/>+ twiddle rotation<br/>+ commutator"] -.- s1
  stg -.- s2
  stg -.- sS
```

With `S = log4 L` stages:

- **The input transposer** (`swap`, `InputTransposeChainStreaming`). A radix-4 butterfly needs
  samples that are a quarter-frame apart -- `x[m]`, `x[m + L/4]`, `x[m + 2L/4]`, `x[m + 3L/4]` -- but
  they arrive on the four lanes in natural order. The transposer is a chain of **commutators**:
  delay lines plus a rotating switch that move samples *between* the lanes over time, so a
  butterfly's four inputs line up in the same cycle. This is the classic element of pipelined FFTs
  (multi-path delay commutator designs).
- **The stages.** Each applies the radix-4 butterflies (multiplies by `±1, ±j` only, so exact), then
  rotates by the twiddle factors `W_L^{mq}` before the next stage -- the only lossy step -- and
  reorders through its own commutator for the next stage's butterflies.
- **The digit-reversal reorder** turns the FFT's digit-reversed output order into natural order.

### Why `VitisFft` is far below the architecture's rate

An SSR FFT with `R` lanes should take a new frame every `L/R` cycles: the commutators shuffle samples
*across* frames, so frame `k+1` enters while frame `k` is still inside. AMD's L1 guide (2020.1) states
exactly that, II = `L/R`. `VitisFft` measures 7.5 `L/R` at `L = 64, 256` and 10 `L/R` from 1024 up --
2,556 cycles a frame at `L = 1024` against a nominal 256. Three things stack, and only the first is
`VitisFft`'s own choice:

1. **The connection.** `VitisFft`'s body calls `fft<>(in, out)`, the guide's *non-streaming
   connection*, which wraps the core in buffer-to-stream blocks and serializes frames. The guide's
   *streaming connection* -- `innerFFT` in a DATAFLOW region between producer and consumer
   processes -- overlaps frames: measured in cosim at `L = 1024`, about 1,420 cycles a frame (5.5
   `L/R`) instead of 2,556. Both compile, both are bit-exact, both synthesize; only a timing
   measurement against the vendor's stated number tells them apart.
2. **The commutators, as functions.** Each commutator in the 2025.1 library runs a fixed
   `L/R + 2(R−1)·PF` iterations per call -- the frame plus a fill-and-drain window -- because an HLS
   process in a dataflow region must return. The drain is paid every frame instead of overlapping the
   next one, which floors the interval at about 2.5 `L/R` whatever the connection. (This window is
   also why an isolated frame's latency depends on its arrival phase -- next section.)
3. **The stage loops and the reorder.** Each stage re-enters its butterfly pipeline once per
   sub-transform, and the digit-reversal reorder fills its buffer and then empties it, one after the
   other.

None of these is a limit of the FFT or of HLS. They are the "accelerate a function call" idiom applied
to an architecture whose point is that nothing stops. Waveflow's own SSR FFT,
`waveflow.dsp.ssr_fft.SsrFft`, keeps this library's arithmetic -- so it is bit-exact with `VitisFft` --
and rebuilds the data movement as free-running tasks: it measures exactly `L/R` cycles a frame at every
length from 16 to 4096, on the same DSPs. The evidence for (1)-(3), with every variant's cosim, is in
`plans/witness/vitis_fft_streaming/`.

### Latency depends on when a frame arrives

The input transposer's commutator runs on a **free-running internal cycle** (40 cycles at
`L = 64`, about `2.5 L/R`), restarting even when no data is arriving. A frame that arrives out of
step with it waits inside the transposer for the cycle to come round; every later stage is shifted by
the same amount. So an isolated frame's latency spreads over a range set by its arrival phase -- 105
to 123 cycles of processing at `L = 64`, 1908 to 2304 at `L = 1024`. Back to back, frames stay in
step and the interval is exact. The first frame after reset is in step by construction, and is the
fastest frame there is.

## The numbers

The arithmetic is modelled bit-exactly (`waveflow.vitis_l1.fft`, one vectorized pass per stage).
Precision is lost in exactly one place: the inter-stage twiddle rotation, which truncates each
partial product into the first operand's format. Everything else grows: each stage's two adder
levels add a bit, so the output is wider than the input --

```
OUTPUT_WL = in_W + log2(L) + 1        # ap_fixed<16,2> in, L = 1024  ->  ap_fixed<27,13> out
```

`VitisFft` derives the output format by asking the model what it produced, and the C++ body
`static_assert`s the same width against the vendor's own type.

## What Waveflow adds

| | |
|---|---|
| the bits | `waveflow.vitis_l1.fft`, bit-exact against the library |
| the module | `VitisFft` -- ports, parameters, `run_iter`, `kernel_task` |
| the C++ | `waveflow/build/vitis_fft_task.h`, copied into the build, not generated |
| the timing and resources | calibrated on `waveflow/calib/platforms/rfsoc4x2_bfm_250mhz` |
| the evidence | `tests/vitis_l1/fft/`, and the XSI gates of `examples/vitis_fft` |

The pages in this section:

- [Parameters](parameters.md) -- constructing a `VitisFft`, and every parameter.
- [Interfaces](interfaces.md) -- the `R`-wide port group, the word packing, and wiring it.
- [Including it in your design](build.md) -- the build steps, and where every file goes.
- [Synthesizing a Vitis L1 block](synthesis.md) -- why the body is copied rather than generated.
- [Latency and II for a vendor block](timing.md) -- the timing model and its calibration.
