---
title: A vendor FFT, frames in and out
parent: Examples
nav_order: 9.57
has_children: true
example_dir: examples/vitis_fft
summary: "AMD's Vitis L1 SSR FFT used as a Waveflow module: four AXI-Stream lanes in, four out, the vendor's own xf::dsp::fft::fft<> as the hardware body and a bit-exact Python model as the simulation. The free-running top is generated from the module and the XSI testbench from the testbench graph; there is no hand-written C++ in the example. Bit-exact at RTL on the RFSoC 4x2. The example is also a worked calibration: the vendor core turns out to be frame-at-a-time with a phase-dependent latency, and the timing and resource models are measured on the platform, with their scope stated and a recipe to extend it."
---
# A vendor FFT, frames in and out

Every earlier example writes its own kernel. This one does not: the arithmetic is AMD's
**Vitis L1 SSR FFT** (`xf::dsp::fft::fft<>`), and the example is about using a vendor block as an
ordinary Waveflow module, with the same guarantees as a block you wrote yourself. The module is
`VitisFft`; [The Vitis FFT](../../guide/vitis_l1/fft/index.md) in the guide describes it. This
example is the end-to-end run, and what measuring it at RTL taught us.

## Learning objectives

- Use a **vendor HLS library block** as a `FreeRunMod`: its C++ body is the vendor call
  ([`kernel_task()`](../../guide/vitis_l1/fft/synthesis.md)), its simulation is a **bit-exact Python
  model** of the vendor's arithmetic, so the two are twins rather than approximations.
- Drive an **`R`-wide port group**: an SSR FFT with `R = 4` takes four samples per cycle on four
  AXI-Stream lanes, sample `n` on lane `n % R`.
- **Measure** a block's timing at RTL rather than trusting a datasheet, a C-synthesis report (which
  reads `undef` for this block) or the block's nominal rate.
- **Calibrate an LT model** for a block whose latency depends on something the model cannot see,
  state its error rather than hide it, and keep the calibration on the platform where every design
  can reuse it.

## The design

```
StreamDriver x 4  ->  VitisFft (L, R = 4)  ->  StreamSink x 4
```

The testbench is a **graph** (`VitisFftTB`, in `waveflow/vitis_l1/testbench.py`): the same object runs
the pysim and, through `tb_top_spec`, generates the XSI harness that drives the RTL. Both read their
stimulus from the same burst bundles and are checked against the same golden.

| | |
|---|---|
| input | `ap_fixed<16, 2>` complex, packed into 32-bit words (real in the low half) |
| output | `ap_fixed<W, I>` complex, `W = 16 + log2 L + 1` (21 at L=16): derived from the model |
| gated length | `L = 16`; calibrated `L = 16 .. 4096` |
| target | RFSoC 4x2, `xczu48dr-ffvg1517-2-e` at 250 MHz |

**There is no hand-written C++ in the example.** The body is the framework's
`waveflow/build/vitis_fft_task.h`, the vendor headers stay in the Vitis install, the `ap_ctrl_none` top
comes from `composite_top_spec` on the module, and the harness from the testbench graph. `include/`,
`gen/`, `xsi/` and `work/` are build output: delete them and the build writes them back. Where each
file comes from is [Including it in your design](../../guide/vitis_l1/fft/build.md).

## Running it

```bash
python -m examples.vitis_fft.vitis_fft                       # pysim: bit-exact + frame timing
python -m examples.vitis_fft.vitis_fft_build                 # generate, then csynth (needs Vitis)
pytest tests/examples/test_vitis_fft_xsi.py -m xsi           # the RTL gates (needs Vivado)
python -m waveflow.calib.fixtures.vitis_fft --work <dir>     # recalibrate the platform
python -m examples.vitis_fft.vitis_fft_figures               # redraw this example's figures
```

## The pages

- [The testbench](test.md) -- the graph, the two scenarios, how the RTL is timed, the gates.
- [Timing](timing.md) -- what the RTL measured, why the core runs at a tenth of its rate and its latency
  depends on arrival phase, the calibrated model and its error, and what XSI costs against it.
- [Resources](resource.md) -- what the core costs per length, and how resource records reach the
  platform.
