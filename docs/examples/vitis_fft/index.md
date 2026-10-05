---
title: A vendor FFT, frames in and out
parent: Examples
nav_order: 9.57
example_dir: examples/vitis_fft
summary: "AMD's Vitis L1 SSR FFT used as a Waveflow module: four AXI-Stream lanes in, four out, the vendor's own xf::dsp::fft::fft<> as the hardware body and a bit-exact Python model as the simulation. The free-running top is generated from the module and the XSI testbench from the testbench graph; there is no hand-written C++ in the example. Bit-exact at RTL on the RFSoC 4x2. The example is also a worked calibration: the vendor core turns out to be frame-at-a-time with a phase-dependent latency, and the page shows how a measured table -- an exact interval, a mean latency and its spread -- gives a pysim that is unbiased and within a stated bound of the RTL."
---
# A vendor FFT, frames in and out

Every earlier example writes its own kernel. This one does not: the arithmetic is AMD's
**Vitis L1 SSR FFT** (`xf::dsp::fft::fft<>`), and the example is about using a vendor block as an
ordinary Waveflow module, with the same guarantees as a block you wrote yourself. The module is
`VitisFft` from `waveflow.vitis_l1`; the [Vitis L1 guide](../../guide/vitis_l1/index.md) explains how it
is built. This page is the end-to-end run, and what measuring it at RTL taught us.

## Learning objectives

- Use a **vendor HLS library block** as a `FreeRunMod`: its C++ body is the vendor call
  ([`kernel_task()`](../../guide/vitis_l1/synthesis.md)), its simulation is a **bit-exact Python
  model** of the vendor's arithmetic, so the two are twins rather than approximations.
- Drive an **`R`-wide port group**: an SSR FFT with `R = 4` takes four samples per cycle on four
  AXI-Stream lanes, sample `n` on lane `n % R`.
- **Measure** a block's timing at RTL rather than trusting a datasheet, a C-synthesis report (which
  reads `undef` for this block) or the block's nominal rate.
- **Calibrate an LT model** for a block whose latency depends on something the model cannot see,
  and state its error rather than hide it.

## The design

```
StreamDriver x 4  ->  VitisFft (L, R = 4)  ->  StreamSink x 4
```

`examples/vitis_fft/vitis_fft.py` declares this as a testbench **graph** (`VitisFftTB`). The same object
runs the pysim and, through `tb_top_spec`, generates the XSI harness that drives the RTL. Both read
their stimulus from the same burst bundles, so both play the same frames with the same gaps between
them, and both are checked against the same golden: the bit-exact model in `waveflow.vitis_l1.fft`.

| | |
|---|---|
| input | `ap_fixed<16, 2>` complex, packed into 32-bit words (real in the low half) |
| output | `ap_fixed<W, I>` complex, `W = 16 + log2 L + 1` (21 at L=16): derived from the model |
| target | RFSoC 4x2, `xczu48dr-ffvg1517-2-e` at 250 MHz |

**There is no hand-written C++ in the example.** The body is the framework's
`waveflow/build/vitis_fft_task.h`, the vendor headers stay in the Vitis install, the `ap_ctrl_none` top
comes from `composite_top_spec` on the module, and the harness from the testbench graph. `include/`,
`gen/`, `xsi/` and `work/` are build output: delete them and the build writes them back.

## Running it

```bash
python -m examples.vitis_fft.vitis_fft                       # pysim: bit-exact + frame timing
python -m examples.vitis_fft.vitis_fft_build                 # generate, then csynth (needs Vitis)
pytest tests/examples/test_vitis_fft_xsi.py -m xsi           # the RTL gate (needs Vivado)
python -m examples.vitis_fft.vitis_fft_build --measure       # recalibrate the timing table
```

The build locates the vendor FFT headers itself (`WF_VITIS_LIBS`, or the Vitis install).

## What the RTL measured: a frame-at-a-time core

Feed frames back to back and the throughput is not what `R = 4` suggests. The core takes `R`
samples per cycle *while a frame is entering*, but does not take the next frame until the current
one is nearly through:

| L | peak rate | frame interval (II) | sustained rate | latency (isolated frame, mean) |
|---|---|---|---|---|
| 16 | 4 / cycle | 41 | 0.39 / cycle | 43 |
| 64 | 4 / cycle | 120 | 0.53 / cycle | 133 |
| 256 | 4 / cycle | 480 | 0.53 / cycle | 519 |

The interval is about the latency: **frames do not overlap**. It is not our wrapper. The vendor's
own array-port core, co-simulated without any of our code, measured 1477 / 1478 at `L = 1024`. And
restructuring the wrapper the way AMD's L2 kernel calls the core -- the frame loop inside one
dataflow region, the dataflow core called once per frame -- changed nothing (42 cycles per frame at
`L = 16`, plus a drain at each batch boundary).

Why, read off the RTL's internal FIFO handshakes: each process in the core runs at about one word per
cycle and needs only ~2.5 `L/R` cycles per frame, so a chain of them *could* overlap frames at an
interval near that. But the core's stages are nested dataflow regions (`fftStage` holds `fftStage_1`
holds `fftStage_2`), and a process holding a nested region is not done until everything inside it is
-- so one frame occupies the whole chain.

**This is a property of how the library is written, not of HLS.** It is designed to be called once
per frame inside a host-launched kernel, and AMD's own route to more throughput is several FFTs in
parallel (`generateFFTKernel`), not frames through one. A Waveflow design that needs `R` samples per
cycle *sustained* -- an RF front end at the converter rate -- needs parallel instances, or an FFT
written as a flat chain of free-running stage modules, which would not have the nesting.

## Why the latency is not one number

Isolated frames do not all take the same time. At `L = 64` they take 121 to 139 cycles; at
`L = 256`, 454 to 548. The trace says where: the core's **input transposer** (`swap`, a chain of
*commutators* -- delay lines plus a rotating switch that move samples between the four lanes) runs on
a **free-running internal cycle**, 40 cycles at `L = 64`, restarting even when no data is arriving. A
frame that arrives out of step with it waits inside the transposer for the cycle to come round; every
stage after it is then shifted by the same amount. The wait is set by the frame's **arrival phase**.

Back to back, the core never idles, frames stay in step, and the interval is exact. The first frame
after reset is in step by construction, which makes it the *fastest* frame (391 cycles at
`L = 256`, below the whole isolated range) and unrepresentative of a running system.

## The timing model, calibrated

An LT model cannot know a frame's arrival phase to the cycle -- its arrival times are themselves
approximations -- so a deterministic phase rule would turn small upstream errors into errors of up to
a whole commutator period. The model is exact where the hardware is phase-independent, and honest
where it is not:

- `ii_cycles` = the back-to-back interval: **exact**.
- `latency_cycles` = the **mean** latency of an isolated frame over a sweep of arrival phases.
- The measured **spread** is the model's stated error.

`--measure` produces those numbers. For each `L` it builds a work copy, runs four frames back to back
and a 48-frame **phase sweep** -- isolated frames whose gaps vary pseudo-randomly over more than one
commutator period, so the arrivals sample every phase -- checks every frame's bits, reads the port
handshakes with Waveflow's VCD parser, and writes `measured/vitis_fft_timing.json`. The testbench reads
its timing from that file.

Checked against the RTL sweep frame by frame (frames 1-47):

| L | pysim − RTL, mean | min .. max | last of 47 frames |
|---|---|---|---|
| 16 | 0.0 | 0 .. 0 | exact |
| 64 | −0.1 | −6 .. +12 | −0.01 % |
| 256 | +0.2 | −29 .. +65 | −0.07 % |

**Unbiased, within the stated bound on every frame, and the error does not accumulate**: it is
per-frame jitter of a known size, not drift. That is the most an LT model can promise for this block.

## The gates

`tests/examples/test_vitis_fft_xsi.py`, under `-m xsi`, at `L = 16`, traced so the RTL and the pysim
share a time origin:

- `test_vitis_fft_rtl_back_to_back_cycles`: frames done at `[43, 84, 125, 166]`, exactly. Every run
  also checks every frame's bits against the golden.
- `test_vitis_fft_pysim_within_the_measured_bound`: back to back, intervals exact; isolated frames,
  each within the measured spread (zero at `L = 16`, so exact here).
- `test_vitis_fft_calibration_table_is_current`: the committed table still describes this RTL, so a
  change that moved the timing fails instead of leaving the pysim configured for old hardware.

`tests/examples/test_vitis_fft.py` keeps the pysim, the table and the generated build tree honest in
the fast suite, with no toolchain.

## See also

- [Vitis L1 Blocks](../../guide/vitis_l1/index.md): the module, its port group and parameters.
- [Synthesis](../../guide/vitis_l1/synthesis.md): how the vendor call becomes the task body.
- [Latency and II](../../guide/vitis_l1/timing.md): the timing model and where its numbers come from.
- [XSI testbench in HLS](../../guide/comp_codegen/xsi_tb.md): how the harness is generated from the graph.
