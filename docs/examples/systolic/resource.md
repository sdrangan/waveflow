---
title: Systolic resource results
parent: A systolic matrix multiply, measured
nav_order: 3
summary: "What the engine and the wrapper cost at the centre configuration on the xczu48dr, from csynth's XML (Vitis 2025.1): 154 DSP, 31k LUT and 36k FF for the engine, 32 to 36 BRAM18K by word width; the wrapper adds 3 DSP, 6k LUT, 5 to 7k FF and 4 to 8 BRAM18K. What the continuous array changed, and where the BRAM goes."
---

# Resources

From csynth's XML (`<top>_proj/solution1/syn/report/csynth.xml`), `xczu48dr-ffvg1517-2-e` at 4 ns, Vitis
HLS **2025.1**, the centre configuration -- from `examples/systolic/measured.json`. These are csynth's
estimates, before Vivado implementation.

| Module | Word | DSP | LUT | FF | BRAM18K | Est. clock |
|---|---|---|---|---|---|---|
| `SystolicEngine` | 64-bit | 154 | 31,316 | 36,233 | 32 | 2.92 ns |
| `SystolicEngine` | 128-bit | 154 | 31,230 | 36,347 | 36 | 2.92 ns |
| `SystolicEngineMem` | 64-bit | 157 | 37,221 | 41,727 | 36 | 3.35 ns |
| `SystolicEngineMem` | 128-bit | 157 | 37,443 | 43,812 | 44 | 3.35 ns |

## What they say

- **The DSPs are the array.** 137 of the engine's 154 are the core: its `R·C = 32` complex multiplies
  at four real multiplies each (128) and its index products; the word width does not touch them. The wrapper
  adds 3 for its address arithmetic.
- **The word width costs BRAM, not logic.** At 128 bits the payload FIFOs carry 129-bit framed words and
  the wrapper's memory ports are twice as wide: 4 more BRAM18K in the engine, 8 in the wrapper; LUT and
  FF barely move.
- **The wrapper costs ~6k LUT and 5 to 7k FF** -- the sequencer, the feeder, the writer and the
  framework's memory writer -- and its clock estimate, 3.35 ns, is the memory side's, not the array's.
- **Running the array continuously** traded registers for logic: the engine went from 36.9k to 31.2k
  LUT (one set of edge counters in place of one per row and column) and from 25.7k to 36.3k FF (the
  delay lines on the edges, the `C` FIFO to the collector, a deeper pipeline), and from 30 to 36 BRAM18K
  at 128 bits.

## What is not here yet

One configuration. A resource model over the engine's parameters -- the array size, the maxima, the
lanes, the widths -- comes with the calibration (`plans/systolic_characterization.md`), fitted on a
set of builds and scored on held-out ones.
