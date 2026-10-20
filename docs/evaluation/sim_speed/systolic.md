---
title: "Simulation speed: the systolic engine"
parent: Simulation speed
nav_order: 3
summary: "PRELIMINARY (measured on a development laptop in use; to be re-run on the lab machine). One bit-exact 8×8×32 complex matrix product run three ways on the same operands: the untimed model (0.036 ms a product), Waveflow's pysim of the engine's six tasks (0.73 ms, 44 SimPy events) and the synthesized RTL under XSI (43 ms, plus a 20 s XSI build and 80 s of csynth per design point). Where pysim's time goes -- Python packing and serialization, not SimPy -- how vectorizing the lane groups halved it, and how the flat pysim (one process, 20 events a product) runs about 1.8 times faster again."
---

# Simulation speed: the systolic engine

**Preliminary.** These numbers were taken on a development laptop that was in use, which only ever
adds time; the final ones come from the same script on the lab machine. The ratios between the three
ways, and the SimPy event counts, are the findings to read now.

## The set-up

The [systolic example](../../examples/systolic/index.md), unchanged: `SystolicEngine` at the centre
configuration (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`), 12-bit complex operands, 128-bit words,
in its [harness](../../examples/systolic/test.md), with `8×8×32` products back to back -- the same
random operands, and the same bit-exact model, for all three:

```text
untimed:  operands --> waveflow.linalg.matmul.matmul (exact sums, one rounding), a product at a time
pysim:    StreamDriver --> SystolicEngine (6 tasks, every channel timed) --> StreamSinks  (SimPy)
RTL:      AxisMaster BFM --> synthesized systolic_engine (6 hls::tasks) --> AxisSlave BFMs  (XSI)
```

- **Untimed** is the model alone: integer numpy over whole matrices.
- **The pysim** is the engine's six tasks -- receiver, two loaders, the core, the collector, the store --
  each a SimPy process, every channel a timed stream or stream of blocks, the core on its
  [calibrated phase](../../examples/systolic/timing.md#the-cores-timed-phase-calibrated). Every run's
  outputs are checked against the model.
- **The RTL** is the csynth'd engine simulated by XSI through the system flow, untraced; csynth and the
  XSI build are reported apart from the simulation.

```bash
python -m examples.systolic.systolic_runtime --rtl --root C:/wfsc/rt   # -> examples/systolic/runtime.json
```

## Results

Per product, fitted over 10, 50 and 100 products; every run exact:

| | per product | vs untimed | one-time cost |
|---|---|---|---|
| untimed model (`matmul`) | **0.036 ms** | 1x | none |
| pysim, the engine's six tasks | **0.73 ms** (44 SimPy events) | 20x | ~56 ms to build the harness |
| pysim, flat (`pysim="flat"`) | about 1.8x faster than the six tasks (20 SimPy events) | -- | the same harness |
| RTL under XSI | **43 ms** | 1,200x | XSI build 20.2 s (compile 1.5, elaborate 14.7, testbench 3.9); csynth 80 s |

Measured 2026-10-20 (Vitis HLS / Vivado xsim 2025.1), the best of three runs a point.

## Insights

- **The RTL is about 60 times the pysim, before its build.** A design point costs 80 s of csynth and 20 s
  of XSI build before the first product; the pysim is ready in 56 ms.
- **The pysim is 20 times the model here, against 1.2 to 2.4 times for the [FFT](fft.md).** Not because
  the simulation is slow -- 44 events a product is few -- but because the model is fast: one `8×8×32`
  product is 36 microseconds of integer numpy, and anything a timed simulation adds is large beside it.
- **The time is Python, not SimPy.** A profile of 50 products put most of the simulation in two places:
  packing and unpacking lane groups one at a time (the core's `C` out in drain order, the collector's
  3,200 `unpack_group` calls), and serializing complex operands and results through `ComplexField`
  arrays (`lanes.to_words`, `from_words`). SimPy's own scheduling was a small part.
- **Vectorizing the lane groups halved it**: `lanes.pack_groups` / `unpack_groups` place each lane's field
  in whole arrays (a loop over the `L` lanes, not over the groups), and pysim went from 1.35 to 0.73 ms
  a product.
- **The flat pysim does less of it.** [`SystolicEngine(pysim="flat")`](../../guide/dsp/systolic/systolic_engine.md#the-flat-pysim)
  runs one process instead of six, with no internal channels: 20 SimPy events a product instead of 44,
  2.2 times fewer Python calls, the same words and the tasks' timing within 2%. Back to back with the
  six-task pysim on the same (busy) machine it ran 1.8 to 1.9 times faster; its time on a quiet machine
  is for the lab run. Trimming the message handling itself bought 2.3% fewer calls -- earlier work had
  taken the obvious wins there.

## Future work

- **A direct path for complex-array serialization** -- the operands into words and `C` back out
  (`lanes.to_words`, `from_words`, about 40 and 75 microseconds a matrix, vectorized but general) --
  about a quarter of the six-task pysim's time.
- **Re-run on the lab machine** for the numbers this page will keep.
