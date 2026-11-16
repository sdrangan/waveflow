---
title: Systolic resource results
parent: A systolic matrix multiply, measured
nav_order: 3
summary: "What the engine and the wrapper cost on the xczu48dr, from csynth's XML (Vitis 2025.1): at the centre 154 DSP, 32k LUT and 36k FF for the engine, 32 to 36 BRAM18K by word width, and the wrapper 3 DSP and 6k LUT more. A sweep of 23 configurations, fifteen of the engine alone and eight of the memory-backed engine at the LTBF system's array shapes: DSPs are four per processing element plus the index products; LUTs and flip-flops grow with the array, about 0.5 to 0.6k LUT and 0.6 to 0.8k FF an element; the maxima and the panels barely move them. A half-size engine costs about what the earlier SystolicUnit did, and runs 2.3 times as fast. The resource model on the platform, a model per task: DSP and BRAM by rule, exact on all 23 builds; LUT and FF within 3.7 and 3.3% mean on the two held out."
---

# Resources

From csynth's XML (`<top>_proj/solution1/syn/report/csynth.xml`), `xczu48dr-ffvg1517-2-e` at 4 ns, Vitis
HLS **2025.1** -- estimates, before Vivado implementation. Every design here meets 4 ns in csynth's
estimate.

## At the centre

From `examples/systolic/measured.json` (`systolic_measure`):

| Module | Word | DSP | LUT | FF | BRAM18K | Est. clock |
|---|---|---|---|---|---|---|
| `SystolicEngine` | 64-bit | 154 | 31,718 | 36,354 | 32 | 2.92 ns |
| `SystolicEngine` | 128-bit | 154 | 31,629 | 36,481 | 36 | 2.92 ns |
| `SystolicEngineMem` | 64-bit | 157 | 37,594 | 41,763 | 36 | 3.35 ns |
| `SystolicEngineMem` | 128-bit | 157 | 37,852 | 43,776 | 44 | 3.35 ns |

- **The word width costs BRAM, not logic.** At 128 bits the payload FIFOs carry 129-bit framed words and
  the wrapper's memory ports are twice as wide: 4 more BRAM18K in the engine, 8 in the wrapper.
- **The wrapper costs ~6k LUT and 5 to 7k FF** -- the sequencer, the feeder, the writer and the
  framework's memory writer -- and its clock estimate, 3.35 ns, is the memory side's, not the array's.
- **Running the array continuously** traded logic for registers: the engine went from 36.9k to 31.2k
  LUT (one set of edge counters in place of one per row and column) and from 25.7k to 36.3k FF (the
  delay lines on the edges, the `C` FIFO to the collector, a deeper pipeline).

## Across configurations

From `examples/systolic/resources.json` (`systolic_sweep`, on the lab machine, 2026-11-06): the engine
alone at 64-bit words, one parameter varied at a time from the centre (`Mmax 8, Kmax 8, Nmax 32, Pmax
4, R 4, C 8, L 4`), and two corners.

| Point | Changed | Elements `R·C` | DSP | LUT | FF | BRAM18K |
|---|---|---|---|---|---|---|
| `small` | `R 2, C 4, L 2, Mmax 4, Nmax 16` | 8 | 59 | 14,317 | 12,534 | 15 |
| `r2` | `R 2, L 2` | 16 | 91 | 20,709 | 23,390 | 23 |
| `c4` | `C 4` | 16 | 91 | 19,506 | 19,391 | 18 |
| `centre` | -- | 32 | 154 | 31,718 | 36,354 | 32 |
| `l8` | `L 8` | 32 | 154 | 27,551 | 30,018 | 42 |
| `k4`, `k16` | `Kmax 4`, `16` | 32 | 154 | 31,567, 31,821 | 36,410, 36,609 | 16, 32 |
| `p1`, `p8` | `Pmax 1`, `8` | 32 | 154 | 30,981, 32,314 | 35,638, 36,844 | 32, 32 |
| `m16`, `n64` | `Mmax 16`, `Nmax 64` | 32 | 154 | 32,424, 31,848 | 36,786, 36,447 | 32, 32 |
| `r8` | `R 8, L 8, Mmax 16` | 64 | 281 | 48,885 | 56,090 | 42 |
| `c16` | `C 16, Nmax 64` | 64 | 281 | 51,048 | 63,187 | 48 |
| `big` | `R 8, C 16, L 8, Mmax 16, Nmax 64`, 128-bit | 128 | 537 | 94,068 | 110,203 | 62 |

The development laptop measured the same fifteen points with the same version (2026-10-20): eleven
identical, four differing in LUT only, by 9 to 153 (`l8`, `big`, `c16`, `r8`).

And the memory-backed engine (`SystolicEngineMem`, `engine_mem_harness`) at the LTBF system's array
shapes, `Mmax 16, Kmax 8, Nmax 8` and 64-bit words, estimated clock 3.35 ns:

| Point | `R × C` | `L` | `Pmax` | DSP | LUT | FF | BRAM18K |
|---|---|---|---|---|---|---|---|
| `ltbf_r2c4_p22`, `p32` | 2 × 4 | 4 | 22, 32 | 62 | 24,325, 25,251 | 21,897, 22,259 | 24 |
| `ltbf_r4c4_p22`, `p32` | 4 × 4 | 4 | 22, 32 | 94 | 28,584, 29,510 | 29,839, 30,201 | 24 |
| `ltbf_r4c8_p22`, `p32` | 4 × 8 | 4 | 22, 32 | 157 | 41,789, 42,715 | 51,177, 51,539 | 24 |
| `ltbf_r8c8_p22`, `p32` | 8 × 8 | 8 | 22, 32 | 283 | 59,248, 60,168 | 76,247, 76,609 | 38 |

What it says:

- **DSPs are the array's, four per element**, plus the index products elsewhere: 59, 91, 154, 281, 537
  for 8, 16, 32, 64, 128 elements. The maxima, the panels and the lanes do not touch them.
- **LUTs and flip-flops follow the array too**, about 0.5 to 0.6k LUT and 0.6 to 0.8k FF an element
  (from the centre's 32 elements to `r8`'s and `c16`'s 64); the maxima and `Pmax` move them by a few
  percent (the accumulator store and the counters).
- **The lanes trade logic for BRAM**: `L = 8` drains more values a step through fewer, wider groups --
  4k fewer LUTs, 10 more BRAM18K.
- **A half-size engine is about the earlier unit's size, and 2.3 times as fast.** `r2` -- 16 elements --
  costs 91 DSP and 20.7k LUT; `SystolicUnit` at the centre, 32 elements, cost 136 DSP and 20.5k LUT.
  At RTL the half-size engine runs `8×8×32` products back to back at **171 cycles** with 64-bit words
  (`half64_steady` in `measured.json`), the centre engine's rate -- the input binds both -- against the
  unit's 390.

## The resource model

`examples/systolic/engine_resources.py` fits a model per task on 21 of these points and files it on the
platform (`waveflow/calib/platforms/xczu48dr_250mhz_vitis2025_1/models/systolic_engine_*`), so
`SystolicEngine.get_rm` and `SystolicEngineMem.get_rm` price a configuration never built for
`estimate_system`, and a composed estimate (`add_rm`, then `compose()`) adds up the same tasks:

- **DSPs are counted per task** (`waveflow.linalg.cost`): the array's four an element, and the index
  products, whose number follows the DSP's 27-bit port (in the core 5 up to 16 elements, 4 at 32, 3
  from 64). Exact on all 23 points.
- **BRAM18K is counted from the buffers and FIFOs** (`engine_bram18`): csynth places every one in the
  kernel's own share, none in a task; a buffer under 1,024 bits in all and a FIFO of 1,024 bits or
  less go to fabric, the rest take `ceil(bits/18)` columns of 1,024 × 18. Exact on all 23 points.
- **LUTs and flip-flops are fitted** per task, on the terms each declares -- the core's accumulators,
  operand pipelines, drain and the partial sums kept between panels; the loaders' banks; the store's
  lanes -- and for each composite's own share on its FIFOs and the bits its buffers hold in
  flip-flops.

| | LUT mean (worst) | FF mean (worst) | DSP, BRAM exact |
|---|---|---|---|
| fit, 21 points | 3.2% (7.5%) | 2.9% (9.9%) | 21 of 21 |
| held out, scored once: `ltbf_r4c4_p32`, `ltbf_r8c8_p32` | 3.7% (7.3%) | 3.3% (6.7%) | 2 of 2 |

The held-out pair was drawn with a fixed seed before any fit, and meets the targets (DSP and BRAM
exact on at least 90% of builds, LUT and FF within 10% mean). The score is not wholly independent:
some candidate core terms' held-out errors were seen before the terms were chosen (by leave-one-out
on the fit points), and the DSP and BRAM rules were read off all 23 reports. The memory wrapper's
tasks were measured at one geometry (64-bit words, `Mmax 16`, `Kmax` = `Nmax` = 8, the LTBF
system's), so another wrapper reads as extrapolated. The first, whole-engine fit (`systolic_sweep
--fit`: DSP, LUT and FF on `R·C` and `R·C/L`, no BRAM) is superseded.
