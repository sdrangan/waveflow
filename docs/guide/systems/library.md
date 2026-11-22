---
title: Library blocks and their models
parent: Waveflow Architecture
nav_order: 0.48
audience: python
api: [SsrFft, SystolicEngine, SystolicEngineMem, VecUnit, MemRStream, MemWStream, Rfdc, Processor, SwFunction, CpuPlatform]
summary: "The catalog to search before designing a block: every reusable Waveflow block, named in the words of the signal processing it does (an FFT or IFFT for OFDM, a Gram matrix HᴴH, applying an MMSE or beamforming filter, a conjugate-gradient or least-squares solve, a dot product, a matched filter, a memory read, the ADC and DAC, software on the A53), with its class, its guide page, its interfaces, and the state of its timing and resource models -- calibrated, a formula, or measured points only. What no block covers (a direct matrix inverse, Cholesky or QR, LDPC, polar or Viterbi decoding, demapping, CORDIC) is listed too, so a design names what it must build. Then how to price a point without synthesis: the formulas, the calls, and the part's capacity. The page an architecture is planned from."
---

# Library blocks and their models

This page lists every block a design can reuse, in the words of the algorithm it computes, with how
well its cost is known. **Reuse what is hard to build well** -- an FFT, a large complex matrix product,
the memory adaptors, the RF converters. **Write a dedicated kernel for simple work** (an element-wise
multiply, a magnitude, a peak search, a reduction), fusing neighbouring steps: it is smaller and faster
than a general unit, and the programmable vector unit is for when programmability itself is the
requirement. Every block below is a kernel or a device in the words of [Kernels](kernels.md) and
[Devices and the DUT](devices.md).

All hardware numbers here are for the packaged platform **`xczu48dr_250mhz_vitis2025_1`**
(`xczu48dr-ffvg1517-2-e`, the RFSoC 4x2's part, 250 MHz, Vitis HLS / Vivado 2025.1); a model filed on
that platform describes that part, clock and tool version only ([Linear-Algebra
Conventions](../dsp/linalg_common.md#cost-data)).

## The blocks

| the algorithm, in your words | block | interfaces | timing known by | resources known by | page |
|---|---|---|---|---|---|
| an **FFT** of length 16 to 4096 (powers of 4): OFDM demodulation, a spectrum, a fast convolution; forward, inverse, or either chosen per frame (`direction="runtime"`) | `SsrFft` (`waveflow.dsp.ssr_fft`) | streams, `R = 4` complex samples a cycle | **by construction**: a frame every `L/4` cycles; latency about `19L/16` cycles, measured per `L` | measured at five lengths (12 to 60 DSP, 5.8k to 34k LUT); no model between them | [The SSR FFT](../dsp/ssr_fft/index.md), [its resources](../../examples/ssr_fft/resource.md) |
| the same FFT as AMD ships it | `VitisFft` (`waveflow.vitis_l1`) | streams | calibrated at RTL on the RFSoC 4x2 | measured | [The Vitis FFT](../vitis_l1/fft/index.md) -- a reference: use `SsrFft` |
| a **complex matrix product** `A·B` or `Aᴴ·B`: a Gram matrix `HᴴH`, a matched filter `Hᴴy`, applying a precomputed filter or beamformer `W·Y`, a correlation | `SystolicEngine` (`waveflow.linalg`) | streams: a command, then `A` and `B`; `C` out | **a formula** (three bounds: compute, input, output) and RTL points; pysim's core phase calibrated | **a model on the platform**: DSP and BRAM by rule, exact; LUT and FF fitted, 3.7 / 3.3 % mean held out | [Systolic engine](../dsp/systolic/systolic_engine.md), [the bounds](../dsp/systolic/systolic_theory.md#three-bounds-on-a-product) |
| the same, for matrices larger than fit on chip, in memory | `SystolicEngineMem` (`waveflow.linalg`) | a job descriptor on a stream; `A`, `B`, `C` over `m_axi` | as the engine, per tile, plus the memory side | a model on the platform (the engine's plus its wrapper) | [Systolic engine behind memory](../dsp/systolic/systolic_engine_mem.md) |
| **programmable vector work** on complex fixed point: element-wise products and sums, dot products and norms, scaling by a scalar, a scalar divide; an **iterative solve** (conjugate gradient for MMSE or least squares, with a matrix product beside it); a **matched filter** | `VecUnit` (`waveflow.vecunit`), programmed in `Xwfvec` | streams: queues of blocks in and out | **calibrated** (`VecTiming` in pysim): CG programs within 1 % held out, other programs up to 15 % | **a model on the platform**: DSP and BRAM exact; LUT 1.9 % mean, FF up to 30 % | [Vector Unit](../dsp/vecunit/index.md), [its timing and resources](../dsp/vecunit/timing.md), [CG as a program](../../examples/vecunit_cg/index.md) |
| reading or writing a buffer in DDR or on-chip memory | `MemRStream`, `MemWStream` (`waveflow.hw.mem_stream`) | streams on one side, `m_axi` on the other (adaptors) | calibrated: the platform's bus law plus a control residual a fixture fits | no model on the platform: measure by csynth | [AXI-MM master](../interface/axi_mm/master.md), [their timing](../timing_model/memstream.md) |
| the **ADC and DAC** of an RFSoC | `Rfdc` (`waveflow.hw.rfdc`) | sample streams to the fabric | a block-rate sample model | the converters are hard blocks | [The RF data converter](../rf/rfdc/index.md) |
| **software** beside the hardware: control, scheduling, a rare path | `Processor` running `SwFunction`s (`waveflow.cpu`) | the bus, through its master | **calibrated** on the A53 at 1.2 GHz for six kernel families (below) | area and leakage from McPAT | [Processor Model](../cpu/index.md), [Using it in a DSE](../cpu/dse.md) |

Not for new designs: `SystolicUnit` and `CgVectorUnit` (retired, [Retired Components](../dsp/legacy/index.md)),
the SIMD core (experimental and frozen, [SIMD Core](../dsp/simdcore/index.md): use the vector unit),
and the Vitis `gemv` (a bit-exact model only, no module).

**Worked designs that are not library blocks**, to copy rather than instantiate: FIR filters
([firblock](../../examples/firblock/index.md), [mm_fir](../../examples/mm_fir/index.md)), a per-block
equalizer ([block_eq](../../examples/block_eq/index.md)), an interleaver
([interleaver](../../examples/interleaver/index.md)), the MIMO-CG detector study
([mimo_cg](../../examples/mimo_cg/index.md), measured with Vitis 2024.1). `waveflow kb examples` lists
them all.

## What no block covers

A design that needs one of these builds it, and its cost is **unknown until a csynth probe** measures
one configuration: no model covers it.

- a **direct** matrix inverse, Cholesky, LU or QR factorization, a triangular solve (the vector unit
  runs conjugate gradient, an iterative solve, as a program; a direct one is not written);
- channel decoding: LDPC, polar, turbo, Viterbi; and soft demapping (LLRs);
- CORDIC, square roots, logarithms, phase (`atan2`); the vector unit's divide is the one non-linear
  operation;
- sorting, a top-`k`, a sphere decoder or any tree search;
- resampling, digital up / down conversion, NCOs (the RFSoC's converters do some of this in hard
  blocks: [The RF data converter](../rf/rfdc/index.md)).

## The processor's calibrated software

The A53 platform (`a53_hpi_1200mhz_gem5v25_1_vitis2025_1`, gem5 v25.1 HPI, aarch64 gcc 13.3.0 from Vitis
2025.1) prices six kernel families from their counters ([Calibration](../cpu/calibration.md)):
`sched_ops.add`, `.delete`, `.reprio`, `.sort` (a ready list), `cdot_q15` (a streaming Q15 dot product,
`n` elements), `gather_hist` (random access over a working set), `dispatch` and `ctx_switch`. A
software baseline for arithmetic is priced by the nearest family -- a dot product by `cdot_q15`, with the
mapping stated ("a complex multiply-accumulate counted as four `cdot_q15` elements") -- or marked
unknown:

```python
from waveflow.cpu.platform import CpuPlatform

plat = CpuPlatform.load()
cdot = plat.sw_function("cdot_q15")
feats, cycles, energy_pj = cdot.price({"n": 1024}, plat.cpu_config(n_cores=1))
print(f"{cycles:.0f} cycles, {cycles / plat.f_clk_hz * 1e6:.2f} us")
```

## Pricing a point without building it

[Design-space exploration](dse.md) prices points by models; before any code exists, the same models
price an architecture's alternatives. A number comes from one of four sources, and says which:

| source | how | example |
|---|---|---|
| a **resource model** on the platform | build the module in Python (no toolchain), then `type(m).get_rm(platform).predict(m)` and `.confidence(m)` | `SystolicEngine` (`waveflow.linalg.cost.platform()`), `VecUnit` |
| a **formula** on a block's page | the page's bound or interval, with its measured overhead | the systolic bounds; `L/R` for the FFT |
| a **lane-loop count** for a kernel you write | one bus word an iteration at `II = 1`: `ceil(n / PF)` cycles a message plus its header; `PF` copies of the datapath to run at the bus rate ([Design patterns for loop optimization](../vectorization/hls/loop_optimization.md)) | a gain, a threshold, a scaling stage |
| a **measured point** on a page | quote the page, its configuration and its tool version | a VecUnit CG solve; an FFT's LUTs |

Anything else is **unknown -- needs a csynth probe**: name the configuration to synthesize, and what
number it settles. A prediction outside a model's measured range reports a confidence other than
`EXACT` or `INTERPOLATED`; quote it.

```python
from waveflow.linalg import cost
from waveflow.linalg.systolic_engine import SystolicEngine
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode

fmt = Format(16, 4, True, QMode.AP_RND, OMode.AP_SAT)
eng = SystolicEngine(name="eng", sim=Simulation(), Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4,
                     word_bits=128, a=fmt, b=fmt, c=fmt)
rm = type(eng).get_rm(cost.platform())
print(dict(rm.predict(eng)), rm.confidence(eng).level.value)
```

## The part

The `xczu48dr` holds (AMD's Zynq UltraScale+ RFSoC product table): **4,272 DSP**, **425,280 LUT**,
**850,560 FF**, **1,080 BRAM36** (2,160 BRAM18), 80 UltraRAM, a quad-core Cortex-A53 (the processor platform models it at 1.2 GHz), and eight ADCs and
eight DACs, of which the RFSoC 4x2 board brings out four ADCs and two DACs. A design that needs more
than about 70 % of a resource may not close timing at 250 MHz -- a rule of thumb, not a model: say so.
