---
title: "Simulation speed: the systolic engine"
parent: Simulation speed
nav_order: 3
summary: "PRELIMINARY (measured on a development laptop in use; to be re-run on the lab machine). One bit-exact 8×8×32 complex matrix product run three ways on the same operands: the untimed model (0.031 ms a product), Waveflow's pysim of the engine's six tasks (1.35 ms, 44 SimPy events) and the synthesized RTL under XSI (43 ms, plus a 20 s XSI build and 80 s of csynth per design point). Where pysim's time goes -- packing lane groups and serializing complex arrays in Python, not SimPy -- and what would bring it down."
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
| untimed model (`matmul`) | **0.031 ms** | 1x | none |
| pysim, the engine's six tasks | **1.35 ms** (44 SimPy events) | 44x | ~39 ms to build the harness |
| RTL under XSI | **43 ms** | 1,400x | XSI build 20.2 s (compile 1.5, elaborate 14.7, testbench 3.9); csynth 80 s |

Measured 2026-10-20 (Vitis HLS / Vivado xsim 2025.1), the best of three runs a point.

## Insights

- **The RTL is 32 times the pysim, before its build.** A design point costs 80 s of csynth and 20 s of
  XSI build before the first product; the pysim is ready in 39 ms.
- **The pysim is 44 times the model here, against 1.2 to 2.4 times for the [FFT](fft.md).** Not because
  the simulation is slow -- 44 events a product is few -- but because the model is: one `8×8×32` product
  is 32 microseconds of integer numpy, and anything a timed simulation adds is large beside it.
- **The time is Python, not SimPy.** A profile of 50 products puts most of the simulation in two places:
  packing and unpacking lane groups one at a time (the core's `C` out in drain order, the collector's
  3,200 `unpack_group` calls), and serializing complex operands and results through `ComplexField`
  arrays (`lanes.to_words`, `from_words`). SimPy's own scheduling is a small part.

## Future work

- **Vectorize the lane-group packing** -- the core's `groups_of` and the collector's unpacking as whole
  numpy arrays, not a Python loop a group -- the same fix that brought the FFT's pysim down
  ([how it got here](fft.md#how-it-got-here)).
- **Re-run on the lab machine** for the numbers this page will keep.
