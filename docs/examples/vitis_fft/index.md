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
python -m waveflow.calib.fixtures.vitis_fft --work <dir>     # recalibrate the platform
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

## The timing model, calibrated on the platform

`VitisFft` is framework infrastructure, so its timing is a property of `(component, part, clock)`,
not of this example. It lives on a **platform** -- `waveflow/calib/platforms/rfsoc4x2_bfm_250mhz`
(the RFSoC 4x2 part at 250 MHz, measured through the XSI BFM) -- and any design composing `VitisFft`
on that platform reloads it with no toolchain run.

There are **two** separately measured delays, so two `TimingModel`s, one target each:

- **proc** -- from a frame's last input word in the block to its last output word out: the delay
  `run_iter` defers the write by. Measured on **isolated** frames.
- **ii** -- the frame interval, measured **back to back**.

Both are **added** to what the channels already charge: a frame's transfer in and out is the streams'
cost, and the fit's residual (`rtl_span - pysim_span + current_dly`) subtracts it out, so the module
never restates a channel's timing.

An LT model cannot know a frame's arrival phase to the cycle -- its arrival times are themselves
approximations -- so the proc model outputs the **mean** over a sweep of phases (the number that
minimizes the squared error), and the measured spread is its stated error. The interval has no
spread: back to back the core stays in step.

**A lookup per length, not a law.** The natural law, `b0 + b1 L + b2 L log2 L`, fits L = 16, 64, 256
exactly and then fails: fit on those three, it predicts L = 1024 14% low (proc) and 20% low (ii).
Between 256 and 1024 Vitis changes the implementation -- the twiddles move into a ROM, a fifth stage
appears -- and the per-sample cost steps from 7.5 to 10 cycles per `L/R`, where it stays at 4096. So each model is a
`LookupCalibModel`: exact where measured, and an unmeasured `L` is refused rather than extrapolated.

| L | proc: mean (min – max) | ii |
|---|---|---|
| 16 | 39 (39 – 39) | 41 |
| 64 | 117.1 (105 – 123) | 120 |
| 256 | 454.8 (390 – 484) | 480 |
| 1024 | 2177.7 (1908 – 2304) | 2556 |
| 4096 | 9932.9 (9009 – 10571) | 10240 |

(cycles at 250 MHz; proc counts the output transfer, which the channel charges, so the stored delay
is `proc - L/R`.)

The calibration is a registered fixture, `waveflow/calib/fixtures/vitis_fft.py`:

```bash
python -m waveflow.calib.fixtures.vitis_fft --work <short dir> --lengths 16 64 256 1024
python -m waveflow.calib.fixtures.vitis_fft --holdout 1024     # the evidence against the law
```

For each length it builds a work copy, runs four frames back to back and a 48-frame **phase sweep**
-- isolated frames whose gaps vary pseudo-randomly over more than one commutator period -- checks
every frame's bits, reads the port handshakes with Waveflow's VCD parser, and collects the firings
onto the platform (committed, so a refit needs no toolchain). It then runs the pysim and refits to a
**fixed point**: the interval interacts with the in-flight bound its own prediction sizes, so it takes
a few passes before the pysim's span is the RTL's. At convergence, pysim and RTL agree to within
0.0 cycles on the mean proc span and the interval at every length.

Use a short `--work` path: csynth fails *silently* from a deep one (the Windows path limit).

## What is calibrated, and how to extend it

The calibration that ships is deliberately **narrow**, and it says so rather than pretending otherwise:

| axis | calibrated | outside it |
|---|---|---|
| platform | RFSoC 4x2 (`xczu48dr`), 250 MHz | another part or clock is another platform |
| length `L` | 16, 64, 256, 1024, 4096 | timing refuses; resources report `UNCALIBRATED` |
| widths | input `ap_fixed<16, 2>`, twiddles `<18, 2>` | a different configuration key: refused until calibrated |
| mode | `NO_SCALING`, natural order, `R = 4` | not modelled at all (the bit-exact model covers these) |

Timing and resources both depend on the widths in ways no formula here captures -- a multiply that
stops fitting one DSP slice, a buffer that spills from LUTRAM into BRAM -- and on `L` across an
implementation change Vitis makes between 256 and 1024. A complete sweep of every axis would be days
of synthesis for configurations nobody may need. That is the point of how Waveflow handles it:
**calibration is a recipe you run for the design space you are exploring**, not a table someone filled
in once.

**The recipe.** Every configuration is one command; it builds, measures, bit-checks, and files the
result beside what is already there:

```bash
# another length at the shipped widths
python -m waveflow.calib.fixtures.vitis_fft --work C:/w --lengths 16384

# another width configuration (lands under its own timing components and resource keys)
python -m waveflow.calib.fixtures.vitis_fft --work C:/w --lengths 64 256 --in-w 12 --in-i 2
```

What it adds to `waveflow/calib/platforms/rfsoc4x2_bfm_250mhz/`:

- `components/vitis_fft_task_<in_w>_<in_i>_<tw_w>_<tw_i>_0_0.proc/` and `.ii/` -- the RTL firings
  (`rtl/L<n>/`), the pysim's, the joined corpus and the fitted lookup (`params.json`);
- `modules/vitis_fft-<key>/resource/records.jsonl` -- the synthesis report attributed to the module,
  keyed by its full parameter set.

Nothing in the module changes: a `VitisFft` built at the new configuration finds its timing on the
platform, and one built at a configuration that is still missing raises with the command to run.

**How an agent uses it.** In design-space exploration the refusal is the signal: an agent proposes a
configuration, builds the pysim, and if `VitisFft` refuses, runs the fixture for exactly that point
before continuing. The expensive step -- synthesis plus two XSI runs -- is paid once per
configuration actually visited, and every later pysim of it, in any design on the platform, costs
nothing. The sweep the user never needed is never run.

**When to replace a lookup with a model.** Once enough configurations are measured, a fit may earn
its place. The fixture's `--holdout` reports how a law fit on some lengths predicts the others; it is
how the `[1, L, L log2 L]` law was rejected here (14-20% off across the implementation change). The
resource side has the same choice in `waveflow.calib.resource_model`: a `PriorResourceModel` where the
physics is known (DSP = 12 per stage boundary at these widths, from 3 DSPs per complex multiply), a
fitted residual for LUT and FF, a lookup elsewhere.

## The gates

`tests/examples/test_vitis_fft_xsi.py`, under `-m xsi`, at `L = 16`, traced so the RTL and the pysim
share a time origin:

- `test_vitis_fft_rtl_back_to_back_cycles`: frames done at `[43, 84, 125, 166]`, exactly. Every run
  also checks every frame's bits against the golden.
- `test_vitis_fft_pysim_within_the_measured_spread`: the pysim, timed from the platform -- back to
  back exactly; isolated frames each within the measured spread (zero at `L = 16`).
- `test_vitis_fft_platform_measurements_are_current`: the platform's committed RTL tables still
  describe this RTL, so a change that moved the timing fails instead of quietly mistiming every
  design on the platform.

`tests/examples/test_vitis_fft.py` keeps the pysim, the platform tables (every shipped length present
and converged; an unmeasured length refused) and the generated build tree honest in the fast suite.

## See also

- [Vitis L1 Blocks](../../guide/vitis_l1/index.md): the module, its port group and parameters.
- [Synthesis](../../guide/vitis_l1/synthesis.md): how the vendor call becomes the task body.
- [Latency and II](../../guide/vitis_l1/timing.md): the timing model and where its numbers come from.
- [XSI testbench in HLS](../../guide/comp_codegen/xsi_tb.md): how the harness is generated from the graph.
