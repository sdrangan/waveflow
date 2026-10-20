---
title: Systolic resource results
parent: A systolic matrix multiply, measured
nav_order: 3
summary: "What the engine and the wrapper cost on the xczu48dr, from csynth's XML (Vitis 2025.1): at the centre 154 DSP, 32k LUT and 36k FF for the engine, 32 to 36 BRAM18K by word width, and the wrapper 3 DSP and 6k LUT more. A sweep of fifteen configurations: DSPs are four per processing element plus about 26; LUTs and flip-flops grow with the array, about 0.66k LUT and 0.8k FF an element; the maxima and the panels barely move them. A half-size engine costs about what the earlier SystolicUnit did."
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

From `examples/systolic/resources.json` (`systolic_sweep`): the engine alone at 64-bit words, one
parameter varied at a time from the centre (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`), and two
corners.

| Point | Changed | Elements `R·C` | DSP | LUT | FF | BRAM18K |
|---|---|---|---|---|---|---|
| `small` | `R 2, C 4, L 2, Mmax 4, Nmax 16` | 8 | 59 | 14,317 | 12,534 | 15 |
| `r2` | `R 2, L 2` | 16 | 91 | 20,709 | 23,390 | 23 |
| `c4` | `C 4` | 16 | 91 | 19,506 | 19,391 | 18 |
| `centre` | -- | 32 | 154 | 31,718 | 36,354 | 32 |
| `l8` | `L 8` | 32 | 154 | 27,560 | 30,018 | 42 |
| `k4`, `k16` | `Kmax 4`, `16` | 32 | 154 | 31,567, 31,821 | 36,410, 36,609 | 16, 32 |
| `p1`, `p8` | `Pmax 1`, `8` | 32 | 154 | 30,981, 32,314 | 35,638, 36,844 | 32, 32 |
| `m16`, `n64` | `Mmax 16`, `Nmax 64` | 32 | 154 | 32,424, 31,848 | 36,786, 36,447 | 32, 32 |
| `r8` | `R 8, L 8, Mmax 16` | 64 | 281 | 48,732 | 56,090 | 42 |
| `c16` | `C 16, Nmax 64` | 64 | 281 | 51,111 | 63,187 | 48 |
| `big` | `R 8, C 16, L 8, Mmax 16, Nmax 64`, 128-bit | 128 | 537 | 94,032 | 110,203 | 62 |

What it says:

- **DSPs are the array's, four per element**, plus about 26 for index products elsewhere: 59, 91, 154,
  281, 537 for 8, 16, 32, 64, 128 elements. The maxima, the panels and the lanes do not touch them.
- **LUTs and flip-flops follow the array too**, about 0.66k LUT and 0.8k FF an element on top of ~9k and
  ~6k; the maxima and `Pmax` move them by a few percent (the accumulator store and the counters).
- **The lanes trade logic for BRAM**: `L = 8` drains more values a step through fewer, wider groups --
  4k fewer LUTs, 10 more BRAM18K.
- **A half-size engine is about the earlier unit's size, and 2.3 times as fast.** `r2` -- 16 elements --
  costs 91 DSP and 20.7k LUT; `SystolicUnit` at the centre, 32 elements, cost 136 DSP and 20.5k LUT.
  At RTL the half-size engine runs `8×8×32` products back to back at **171 cycles** with 64-bit words
  (`half64_steady` in `measured.json`), the centre engine's rate -- the input binds both -- against the
  unit's 390.

## What is not here yet

A resource model fitted over these points and scored on held-out configurations, as the earlier unit's
was; the points above are its first builds.
