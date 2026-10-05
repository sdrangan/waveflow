---
title: A vendor FFT, frames in and out
parent: Examples
nav_order: 9.57
example_dir: examples/vitis_fft
summary: "AMD's Vitis L1 SSR FFT used as a Waveflow module: four AXI-Stream lanes in, four out, the vendor's own xf::dsp::fft::fft<> as the hardware body and a bit-exact Python model as the simulation. Four frames run back to back, which is what makes latency and initiation interval separately visible. The free-running top is generated from the module, the XSI testbench from the testbench graph, and there is no hand-written C++ in the example. Bit-exact at RTL; the measured 44-cycle latency and 42-cycle interval put every pysim frame on the RTL's cycle."
---
# A vendor FFT, frames in and out

Every earlier example writes its own kernel. This one does not: the arithmetic is AMD's
**Vitis L1 SSR FFT** (`xf::dsp::fft::fft<>`), and the example is about using a vendor block as an
ordinary Waveflow module, with the same guarantees as a block you wrote yourself. The module is
`VitisFft` from `waveflow.vitis_l1`; the [Vitis L1 guide](../../guide/vitis_l1/index.md) explains how it
is built. This page is the end-to-end run.

## Learning objectives

- Use a **vendor HLS library block** as a `FreeRunMod`: its C++ body is the vendor call
  ([`kernel_task()`](../../guide/vitis_l1/synthesis.md)), its simulation is a **bit-exact Python
  model** of the vendor's arithmetic, so the two are twins rather than approximations.
- Drive an **`R`-wide port group**: an SSR FFT with `R = 4` takes four samples per cycle on four
  AXI-Stream lanes, sample `n` on lane `n % R`.
- See why **one frame is not enough** to check a pipelined block: only frames back to back separate
  its latency from its initiation interval (II).
- **Measure** a block's timing from the RTL waveform, and configure the pysim with it — rather than
  trusting a datasheet estimate or a C-synthesis report (which reads `undef` for this block).

## The design

```
StreamDriver x 4  ->  VitisFft (L = 16, R = 4)  ->  StreamSink x 4
```

`examples/vitis_fft/vitis_fft.py` declares this as a testbench **graph** (`VitisFftTB`). The same object
runs the pysim and, through `tb_top_spec`, generates the XSI harness that drives the RTL. Both read
their stimulus from the same burst bundles under `vectors/`, and both are checked against the same
golden: the bit-exact model in `waveflow.vitis_l1.fft`.

| | |
|---|---|
| input | `ap_fixed<16, 2>` complex, packed into 32-bit words (real in the low half) |
| output | `ap_fixed<21, 7>` complex, 42-bit words: the width grows with `log2 L`, derived from the model |
| frame | 16 samples = 4 words per lane |
| scenario | 4 random full-scale frames, back to back |

**There is no hand-written C++ in the example.** The body is the framework's
`waveflow/build/vitis_fft_task.h`, the vendor headers stay in the Vitis install, the `ap_ctrl_none` top
comes from `composite_top_spec` on the module, and the harness from the testbench graph. `include/`,
`gen/`, `xsi/` and `vectors/` are all build output: delete them and the build writes them back.

## Running it

```bash
python -m examples.vitis_fft.vitis_fft                  # pysim: bit-exact + frame timing, no toolchain
python -m examples.vitis_fft.vitis_fft_build            # generate, then csynth (needs Vitis)
pytest tests/examples/test_vitis_fft_xsi.py -m xsi      # the RTL gate (needs Vivado)
```

The build locates the vendor FFT headers itself (`WF_VITIS_LIBS`, or the Vitis install); set
`WF_VITIS_LIBS` to the directory holding `vitis_fft/` if it cannot.

## What the RTL measured

The XSI gate drives the generated top through four frames. Bits: **every frame bit-exact** against
the golden. Timing, read off the waveform as `TVALID && TREADY` on the ports (clock edges counted from
the start of the simulation, reset included):

| frame | first input beat | last output beat |
|---|---|---|
| 0 | 18 | 61 |
| 1 | 22 | 103 |
| 2 | 26 | 145 |
| 3 | 66 | 187 |

Three facts follow.

**Latency is 44 cycles**, first input word to last output word of a frame (61 − 18 + 1).

**Frames leave every 42 cycles, and do not overlap in the core.** A new frame's output starts only as
the previous one finishes. The FFT's own estimate would be `II ≈ L/R = 4`; the block as built is ten
times slower than that, and nothing short of a measurement shows it. Vitis co-simulation of an
`ap_ctrl_hs` top measured 46, not 42: those four cycles were that top's per-call adapter, which the
free-running top does not have.

**The input side runs ahead.** Frames 0–2 are accepted back to back at cycles 18–29, because the
body's input lanes and their FIFOs hold about two frames; frame 3 waits until frame 0 has left. So
the hardware applies its interval at the core and the output, not at intake.

## The pysim, on the RTL's cycle

`VitisFft` models a pipelined block with three numbers (see
[Latency and II](../../guide/vitis_l1/timing.md)). Configured with the measured ones —
`latency_cycles=44`, `ii_cycles=42` — the pysim finishes the four frames at cycles 44, 86, 128 and 170;
the RTL's sink sees 45, 87, 129, 171. **The difference is one cycle, the same for every frame**: the
harness's first input beat lands one cycle after the pysim's `t = 0`. The gate asserts exactly that,
so a wrong II cannot hide behind the offset.

One thing the model does not reproduce is the input running ahead: the pysim paces intake by the II,
so in a chain it would hold an upstream producer back a little longer than the hardware does. Output
timing, which is what downstream blocks see, is exact.

## The gates

`tests/examples/test_vitis_fft_xsi.py`, under `-m xsi`:

- `test_vitis_fft_rtl_bit_exact`: every output frame equals the golden.
- `test_vitis_fft_rtl_cycles`: the frame completion cycles, `[45, 87, 129, 171]`, exactly. A count
  that moves is a real change, in either direction, and deserves a look.
- `test_vitis_fft_pysim_matches_rtl`: pysim and RTL agree frame by frame, up to the one-cycle offset.

`tests/examples/test_vitis_fft.py` keeps the pysim and the generated build tree honest in the fast
suite, with no toolchain.

## See also

- [Vitis L1 Blocks](../../guide/vitis_l1/index.md): the module, its port group and parameters.
- [Synthesis](../../guide/vitis_l1/synthesis.md): how the vendor call becomes the task body.
- [Latency and II](../../guide/vitis_l1/timing.md): the timing model and where its numbers come from.
- [XSI testbench in HLS](../../guide/comp_codegen/xsi_tb.md): how the harness is generated from the graph.
