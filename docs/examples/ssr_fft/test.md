---
title: SSR FFT testbench
parent: Waveflow's FFT, at full rate
nav_order: 1
summary: "SsrFftTB, the testbench graph both the pysim and the XSI harness are built from -- a stream driver, the FFT, a sink, one RadixWord a beat (or VitisFft's four lanes each way with lanes=True). The scenarios (frames back to back; isolated frames with random gaps; 500 frames), how the RTL is timed from the BFMs with no waveform, how every run checks every frame, and the gates at three levels: the model, csim, and RTL."
---

# The testbench

## One graph, two backends

```
StreamDriver  ->  SsrFft  ->  TimedSink
```

`SsrFftTB` (`waveflow/dsp/ssr_fft/testbench.py`) drives the FFT's `RadixWord` input from one burst
bundle (`vectors/s_in`, one burst per frame, each word four complex samples packed by the schema) and
captures its output in another (`vectors/m_out`). The golden is the bit-exact vendor model on the same
frames `VitisFft`'s testbench draws. With `lanes=True` it is `VitisFftTB` with the other DUT: four
drivers, four sinks, the same lane bundles. It is a `FreeRunMod` whose `potential_targets` is
`SEQUENTIAL_XSI_TB`, so the generator lowers it to an XSI harness, every participant onto a
cycle-exact C++ model:

| Python participant | XSI model | role |
|---|---|---|
| `StreamDriver` | `AxisMaster(..., chunks=2)` | plays the bundle, one burst per frame; 128-bit words as two 64-bit chunks |
| `SsrFft` | the synthesized RTL: 7 to 20 `hls::task`s | the DUT |
| `TimedSink` | `AxisSlave(..., chunks=3)` | collects every word, with the cycle it arrived (184 bits at `L = 64`) |

The `chunks` argument is the only thing the wide port changes: the generator passes it from the
endpoint's width, and the bundles say the same in `meta.json` (`word_bytes`), so a scenario and its
capture cannot disagree with the port they drive.

In the pysim, the DUT is its children: one SimPy process per task, each calling the model's function
for its block.

## The scenarios

- **Back to back.** Every frame queued from the start: measures the interval, and the first frame's
  latency.
- **Isolated frames.** A random idle gap before each frame, long enough to empty the pipeline:
  measures whether latency depends on when a frame arrives (it does not -- [Timing](timing.md)), and
  exercises the drain: the end of every burst has to leave the pipeline with nothing behind it.
- **Long runs.** 500 frames back to back, and 64 frames with random gaps, at `L = 64`, both reorders
  -- all out, all bit-exact, the interval constant. A pipeline whose buffering or flow control is
  wrong tends to stall only after it fills, which a short run does not reach. (Run once, 2026-10-06;
  not part of `ssr_fft_measure`.)

Gaps are a property of the driver in both backends (`StreamDriver.burst_gaps`, a `DynParam` the
harness emits as a member assignment on the C++ `AxisMaster`), so pysim and RTL space the bursts
identically.

## Timing the RTL without a waveform

Each `AxisMaster` dumps the words it handed over with their acceptance cycles, each `AxisSlave` the
words it received with their arrival cycles, on one cycle count. `rtl.frame_times` reduces those to
per-frame `in`, `last_in` and `done`. So a measurement run is untraced. The trace
(`rtl.run_xsi(root, trace=True)`) is for looking inside: with plain-named tasks, every internal
channel's handshake signals are in the VCD by name -- which is how the stranded burst tail was
found (the first stage had read 128 words and written 125).

## Every run checks every frame

`rtl.check_bits` compares every frame the RTL emitted against the golden, and a run with a missing
frame fails as surely as one with a wrong bit: "5 frames out of 8, all correct" was the signature of
the stranded-tail bug.

## The gates

| level | file | what |
|---|---|---|
| model | `tests/dsp/ssr_fft/test_model.py` | the per-stage pipeline equals `fft_general` at `L = 16 .. 4096`; each commutator's tick-by-tick algorithm equals the block transpose, with gaps |
| pysim | `tests/dsp/ssr_fft/test_hw.py` | the composite is bit-exact (both reorders); its first frame within 5 cycles of the RTL's, its interval exact; the build tree is generated |
| csim | `tests/dsp/ssr_fft/test_hls_chain.py` (`-m vitis`) | the task chain, bit-exact frame after frame at `L = 16, 64, 1024`; every loop II = 1 |
| RTL | `tests/dsp/ssr_fft/test_xsi.py` (`-m xsi`) | `L = 64`, both reorders: every frame out and bit-exact, the interval exact (16 / 21), timing met at 250 MHz |

The other lengths, the isolated-frame runs and the long runs are measurements rather than gates:
`ssr_fft_measure` reruns them and rewrites `examples/ssr_fft/measured.json`, which the figures and
the tables in these pages are drawn from.
