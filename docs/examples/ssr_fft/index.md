---
title: Waveflow's FFT, at full rate
parent: Examples
nav_order: 9.565
has_children: true
example_dir: examples/ssr_fft
summary: "Waveflow's own SSR FFT (SsrFft): four AXI-Stream lanes in, four out, a new frame every L/R cycles -- the rate the architecture is built for and 7.5 to 10 times AMD's Vitis L1 core as shipped -- bit-exact with that core. Built as free-running Waveflow modules: one hls::task per commutator, stage and buffer, the top generated from the module graph, the XSI testbench from the testbench graph. Measured at RTL on the RFSoC 4x2 from L = 16 to 4096: the interval, a latency that does not depend on arrival, the pysim against the RTL, and what the throughput costs in resources."
---
# Waveflow's FFT, at full rate

A streaming FFT with four lanes should take a new frame every `L/R` cycles, forever: that is what
its commutators are for. AMD's Vitis L1 FFT, as shipped, takes 7.5 to 10 times longer
([why](../../guide/vitis_l1/fft/index.md#why-vitisfft-is-far-below-the-architectures-rate)).
`SsrFft` keeps that library's arithmetic -- so its output is bit-exact with it -- and rebuilds the
data movement in Waveflow, one free-running task per piece of hardware. This example is the
end-to-end run at RTL. [The SSR FFT](../../guide/dsp/ssr_fft/index.md) in the guide describes the
module.

## Learning objectives

- Build a **streaming DSP block as a composite** of free-running tasks -- here 8 to 18 of them --
  whose top is generated from the module graph, and whose pysim is one process per task.
- See what makes a pipeline run at **one word per cycle without stopping**: no per-frame function
  to return from, frames visible only as a counter, and no blocking read at a loop head.
- Keep the **bits single-sourced** across three backends: one Python model, a pysim that calls it per
  block, and C++ whose formats and tables are generated from the same geometry.
- **Measure** a block at RTL -- interval, latency, isolated-frame latency, resources -- and compare it
  to the vendor core on the same platform.

## The design

```
StreamDriver x 4  ->  SsrFft (L, R = 4)  ->  StreamSink x 4
```

The testbench is the same graph as the [Vitis FFT example](../vitis_fft/index.md)'s, with the other
DUT: the two modules share a port group, so they share the scenario files, the lane packing and the
golden.

| | |
|---|---|
| input | `ap_fixed<16, 2>` complex, packed into 32-bit words (real in the low half) |
| output | `ap_fixed<W, I>` complex, `W = 16 + log2 L + 1`: derived from the model |
| gated length | `L = 64`, both reorders (`tests/dsp/ssr_fft/test_xsi.py`); measured `L = 16 .. 4096` |
| target | RFSoC 4x2, `xczu48dr-ffvg1517-2-e` at 250 MHz |

**There is no hand-written C++ in the example**, and none from a vendor: the task bodies are the
framework's (`waveflow/dsp/ssr_fft/src/`), the per-configuration header and task wrappers are
generated, the top comes from `composite_top_spec` on the module and the harness from the testbench
graph. `include/`, `gen/`, `xsi/` and `vectors/` are build output.

## Running it

```bash
python -m examples.ssr_fft.ssr_fft                       # pysim: bit-exact + frame timing
python -m examples.ssr_fft.ssr_fft_build                 # generate, then csynth (needs Vitis)
pytest tests/dsp/ssr_fft/test_xsi.py -m xsi              # the RTL gates (needs Vivado)
python -m examples.ssr_fft.ssr_fft_measure               # measure every length at RTL -> measured.json
python -m examples.ssr_fft.ssr_fft_figures               # redraw this example's figures
```

## The pages

- [The testbench](test.md) -- the graph, the scenarios, how the RTL is timed, the gates.
- [Timing](timing.md) -- the interval at every length, a latency that does not depend on arrival,
  and the pysim against the RTL.
- [Resources](resource.md) -- what the full rate costs, against the vendor core.
