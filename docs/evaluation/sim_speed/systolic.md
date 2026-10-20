---
title: "Simulation speed: the systolic engine"
parent: Simulation speed
nav_order: 3
summary: "PRELIMINARY (measured on a development laptop in use; to be re-run on the lab machine). Bit-exact complex matrix products from 32×32×32 to 256×32×256 run three ways on the same operands: the untimed model (0.08 to 6.5 ms), Waveflow's flat pysim of the memory-backed engine (57 ms to 1.5 s, cycle counts 5-6% under the RTL's) and the synthesized RTL under XSI (1.5 to 75 s, after 224 s of csynth and 140 s of XSI build). The RTL takes 27 to 50 times as long as the pysim. Then how the pysim got there: vectorized lane groups, the flat pysim (one process), and a direct path for converting matrices to words."
---

# Simulation speed: the systolic engine

**Preliminary.** These numbers were taken on a development laptop that was in use, which only ever
adds time, and single points moved by up to 2× between runs; the final ones come from the same script on
the lab machine. The ratios between the three ways, the cycle counts and the SimPy event counts are the
findings to read now.

## The set-up

The [systolic example](../../examples/systolic/index.md), unchanged: `SystolicEngineMem` at the centre
configuration (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`), 12-bit complex operands, 128-bit words,
in its [harness](../../examples/systolic/test.md): one job, `C = A·B` with `A` of `n × 32` and `B` of
`32 × n`, operands and result in the harness's memory. `K` is fixed at 32, the most the wrapper sums on
chip (`Pmax·Kmax`). The same random operands, and the same bit-exact model, for all three:

```text
untimed:  operands --> waveflow.linalg.matmul.matmul (exact sums, one rounding)
pysim:    job stream + memory model --> SystolicEngineMem (engine pysim="flat") --> memory  (SimPy)
RTL:      AxisMaster + AXI-MM memory BFMs --> synthesized systolic_engine_mem --> memory     (XSI)
```

- **Untimed** is the model alone: integer numpy over whole matrices.
- **The pysim** is the wrapper's tasks (sequencer, feeder, writer, memory writer) around the engine's
  [flat pysim](../../guide/dsp/systolic/systolic_engine_mem.md), timed against the core's
  [calibrated phase](../../examples/systolic/timing.md#the-cores-timed-phase-calibrated). Its time includes
  building the harness. Every `C` word it writes is checked against the model.
- **The RTL** is the csynth'd wrapper simulated by XSI through the system flow, untraced; every `C` word
  checked. csynth and the XSI build are reported apart from the simulation.

```bash
python -m examples.systolic.systolic_runtime --sizes --rtl --root C:/wfsc/rs   # -> runtime.json, "sizes_w128"
```

## Results across sizes

One product per size; every run exact, in pysim and in RTL:

| product | RTL cycles | untimed | pysim (flat) | RTL under XSI | RTL ÷ pysim |
|---|---|---|---|---|---|
| 32×32×32 | 1,609 | 0.08 ms | **57 ms** (346 events) | **1.5 s** | **27×** |
| 64×32×64 | 5,881 | 1.1 ms | **94 ms** (1,210 events) | **2.6 s** | **27×** |
| 128×32×128 | 22,969 | 3.9 ms | **634 ms** (4,666 events) | **19 s** | **30×** |
| 256×32×256 | 91,321 | 6.5 ms | **1.5 s** (18,490 events) | **75 s** | **50×** |

One-time costs, per design point: csynth 224 s; the XSI build 140 s the first time (compile 5.6,
elaborate 41, testbench 93), then 6 to 9 s for each new workload. The pysim needs neither.

Measured 2026-10-20 (Vitis HLS / Vivado xsim 2025.1); pysim and untimed are the best of seven runs, the
RTL one run a size.

**How the RTL column is counted.** The harness has no host to end the RTL run, so XSI simulates a fixed
number of cycles, set 20% (plus 2,000 cycles) past the pysim's length. The table charges the RTL only for
the cycles the product took: its simulation time scaled by `done cycle ÷ cycles run` (for 256×32×256,
86.9 s for 105,725 cycles, of which 91,321 were the product). That favours the RTL: the fixed start-up of
a run is scaled down with it, and the idle cycles after the product, which XSI simulates cheaply, are
charged at the busy rate. The raw times are in `runtime.json`.

## Insights

- **The RTL takes 27 to 50 times as long as the pysim, before its 6 minutes of build.** At the largest
  size that is 75 s against 1.5 s. A change to the design costs a new csynth and XSI build on the RTL
  side, and nothing on the pysim side.
- **The pysim's timing is close.** Its cycle counts are 5 to 6% under the RTL's at every size (1,514
  against 1,609 up to 86,438 against 91,321), so a design question about throughput can be answered at
  pysim speed and confirmed once in RTL.
- **Both grow with the work, about linearly in the RTL's cycles.** The pysim runs 200 to 215 SimPy
  events per thousand RTL cycles at every size; XSI simulates about 1,200 cycles a second on this machine
  at the large sizes.
- **The untimed model is about 80 to 700 times faster than the pysim** (the spread is mostly the
  laptop's noise on sub-millisecond runs). It does only the arithmetic, which
  integer numpy does over whole matrices at once; the pysim also models every transfer, in time. Use the
  model for numerics and the pysim for timing.

## The engine alone, product by product

The engine without its memory wrapper, `8×8×32` products back to back (fitted over 10, 50 and 100
products), against the engine's six-task pysim and its flat one:

| | per product | vs untimed | one-time cost |
|---|---|---|---|
| untimed model (`matmul`) | **0.036 ms** | 1x | none |
| pysim, the engine's six tasks | **0.73 ms** (44 SimPy events) | 20x | ~56 ms to build the harness |
| pysim, flat (`pysim="flat"`) | about 1.8x faster than the six tasks (20 SimPy events) | -- | the same harness |
| RTL under XSI | **43 ms**, see below | 1,200x | XSI build 20.2 s (compile 1.5, elaborate 14.7, testbench 3.9); csynth 80 s |

Measured 2026-10-20 (Vitis HLS / Vivado xsim 2025.1), the best of three runs a point.

The RTL row ran a fixed bound of `400·n + 4000` cycles for products that take 94 cycles each, so most of
its cycles were idle. XSI is event-driven and simulates idle cycles cheaply, so 43 ms is not "94 cycles'
worth"; the size table above bounds the run tightly instead. Two later runs of this table with a tight
bound gave 52 and 129 ms a product, on the laptop at its busiest (in the second run the flat pysim even
measured slower than the six tasks, against every earlier run), so this row waits for the lab machine.

```bash
python -m examples.systolic.systolic_runtime --rtl --root C:/wfsc/rt   # -> runtime.json, "steady_w128"
```

## How the pysim got faster

- **The time is Python, not SimPy.** A profile of 50 engine products put most of the simulation in two
  places: packing and unpacking lane groups one at a time (the core's `C` out in drain order, the
  collector's 3,200 `unpack_group` calls), and converting complex operands and results to and from words
  through `ComplexField` arrays (`lanes.to_words`, `from_words`). SimPy's own scheduling was a small part.
- **Vectorizing the lane groups halved it**: `lanes.pack_groups` / `unpack_groups` place each lane's field
  in whole arrays (a loop over the `L` lanes, not over the groups), and the six-task pysim went from 1.35
  to 0.73 ms a product.
- **The flat pysim does less of it.** [`SystolicEngine(pysim="flat")`](../../guide/dsp/systolic/systolic_engine.md#the-flat-pysim)
  runs one process instead of six, with no internal channels: 20 SimPy events a product instead of 44,
  2.2 times fewer Python calls, the same words and the tasks' timing within 2%. Back to back with the
  six-task pysim on the same machine it ran 1.8 to 1.9 times faster. Trimming the message handling itself
  bought 2.3% fewer calls -- earlier work had taken the obvious wins there.
- **A direct path converts matrices to words**, below.

### Converting matrices to words

Every operand goes into message words, and every `C` comes back out, through `lanes.to_words` and
`from_words`. In general that is a `DataArray` of `ComplexField` elements: vectorized, but several passes
through intermediate forms and a fixed cost to set the array up. When a lane is 8, 16 or 32 bits wide,
the words are simply the bytes of the `(re, im)` integer pairs, so the **direct path** reinterprets the
buffer: one pass, plus a multiply or shift when the register format is narrower than the lane. Both give
the same words (tested bit for bit, and checked again on every size below).

An `n × n` matrix in the engine's operand format (12 bits in 16-bit lanes, 128-bit words), per call,
general path → direct path:

| matrix | elements | `to_words` (µs) | speed-up | `from_words` (µs) | speed-up |
|---|---|---|---|---|---|
| 8×8 | 64 | 116 → 22.4 | **5×** | 199 → 31.1 | **6.4×** |
| 16×16 | 256 | 187 → 17.5 | **11×** | 266 → 49.1 | **5.4×** |
| 32×32 | 1,024 | 243 → 19.4 | **12×** | 383 → 90.5 | **4.2×** |
| 64×64 | 4,096 | 335 → 42.0 | **8×** | 564 → 109 | **5.2×** |
| 128×128 | 16,384 | 522 → 133 | **4×** | 843 → 200 | **4.2×** |
| 256×256 | 65,536 | 4,171 → 241 | **17×** | 6,542 → 1,206 | **5.4×** |
| 512×512 | 262,144 | 12,684 → 1,632 | **8×** | 20,773 → 6,046 | **3.4×** |

Measured 2026-10-20, best of nine batches a point; single points moved by up to 5× between runs (the
8×8 direct `to_words` was 3.4 µs on one run, 22 µs on the next).

```bash
python -m examples.systolic.systolic_runtime --conversion   # -> runtime.json, "conversion_w128"
```

- **The speed-up holds at every size, with no clear trend**: about 4 to 17 times into words and 3 to 6
  times back. At small sizes the direct path saves the general one's fixed set-up cost; at large sizes,
  its passes over the data.
- **Reading back gains less.** `from_words` still copies the lanes out into the two `int64` arrays its
  callers expect, shifts them, and checks no fraction bits are lost. Those passes are most of its time
  at large sizes.
- **Simulation calls:** on four back-to-back engine products, the flat pysim makes 13% fewer Python calls
  (12,756 → 11,084) and the six-task pysim 7% fewer. Its matrices are small, so the per-call set-up was
  what it paid.

## Future work

- **Re-run on the lab machine** for the numbers this page will keep: all three tables.
- **A host model that ends the RTL run** when the last job is done, so XSI simulates only the product's
  cycles and no scaling is needed.
- **Larger `K`**, once the wrapper sums `K > Pmax·Kmax` through memory.
