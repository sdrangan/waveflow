---
title: The SSR FFT
parent: DSP Blocks
nav_order: 1
has_children: true
audience: python
api: [SsrFft, Geometry]
summary: "SsrFft (waveflow.dsp.ssr_fft): a streaming FFT taking R = 4 complex samples a cycle and a new frame every L/R cycles, bit-exact with AMD's Vitis L1 FFT and 7.5 to 10 times its rate as shipped. What L and R mean, the measured interval against the vendor core, and a guide to the section: architecture, parameters, interfaces, build, synthesis, timing."
---

# The SSR FFT

`SsrFft` (`waveflow.dsp.ssr_fft.hw`) is a fixed-point, forward, natural-order FFT of length `L` that
takes `R` complex samples a cycle -- and **a new frame every `L/R` cycles, indefinitely**. It computes
the same bits as AMD's Vitis L1 SSR FFT ([`VitisFft`](../../vitis_l1/fft/index.md)) and has the same
ports; it is built as Waveflow modules, one free-running `hls::task` per piece of hardware.

- **`L`** is the transform length, fixed at build time. It must be a power of `R` with at least two
  stages: 16, 64, 256, 1024, 4096, ...
- **`R`** is the **super-sample rate**: the number of parallel lanes in and out. It is also the
  butterfly radix, and the module supports `R = 4`.

Sample `n` of a frame travels on lane `n % R` at word `n // R`, so a frame enters in `L/R` cycles --
and, back to back, the next one enters right behind it.

Measured in RTL (XSI, RFSoC 4x2 at 250 MHz, every frame bit-exact):

| L | `SsrFft` cycles a frame | `VitisFft` cycles a frame | DSP (both) |
|---|---|---|---|
| 16 | **4** | 41 | 12 |
| 64 | **16** | 120 | 24 |
| 256 | **64** | 480 | 36 |
| 1024 | **256** | 2,556 | 48 |
| 4096 | **1,024** | 10,240 | 60 |

## The pages

- [Architecture](architecture.md) -- the transposer, the stages, the commutators and what they are,
  the digit reversal, why nothing stops between frames, and the arithmetic.
- [Parameters](parameters.md) -- constructing an `SsrFft`, and every parameter.
- [Interfaces](interfaces.md) -- the lane ports, the `RadixWord` streams inside, and wiring it.
- [Including it in your design](build.md) -- the build steps, and where every file goes.
- [Synthesis](synthesis.md) -- the task bodies, the two reorders, and the traps found at RTL.
- [Timing](timing.md) -- an interval by construction, a latency by addition, and the pysim against
  the RTL.

The worked example, with every measurement, is [Waveflow's FFT, at full rate](../../../examples/ssr_fft/index.md).
