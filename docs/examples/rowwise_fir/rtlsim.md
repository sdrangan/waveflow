---
title: C and RTL simulation
parent: Rowwise FIR
nav_order: 6
has_children: false
audience: hls
summary: "Vitis C-simulation and RTL co-simulation of the generated FIR kernel, bit-exact against the one golden, driven by the build DAG (codegen / csim / cosim steps). The Phase-1 hand-HLS sandbox is the ground-truth that first proved compute II=1 and the full-duplex single-bundle read+write."
---

# C and RTL simulation

The generated kernel ([the dataflow hook](./kernel_hook.md) wrapped in the `m_axi` top) is validated
in Vitis the same way every Waveflow example is: **C-simulation**, then **C-synthesis**, then **RTL
co-simulation**, each checked against the one [`execute` golden](./fir.md#the-golden).

## Driven by the build DAG

The example does not hand-roll a build script — it uses the shared
[build DAG](../../guide/build/) ([`fir_build.py`](../../../examples/rowwise_fir/fir_build.py)), a set of
discoverable named steps:

```
codegen → pysim → pytiming → csim → cosim → calibrate
```

- **`codegen`** emits the `m_axi` top, the hook, and the testbench.
- **`csim`** runs Vitis C-simulation: the kernel over numpy fixtures, compared to `fir_golden`.
- **`cosim`** synthesizes and runs RTL co-simulation, producing the VCD the
  [timing extraction](./cosim_timing.md) reads.

## Bit-exact, not tolerance

Because the kernel accumulates taps in the **same left-to-right order** as the golden (float adds are
not associative), csim and cosim match the golden **bit-for-bit** — no tolerance. The verdict is
recorded in [`results/csim_verdict.json`](../../../examples/rowwise_fir/results/). Bit-exactness is
what lets the *timing* study that follows trust that the RTL and the sim are computing the *same*
thing, so any difference is purely timing.

## The Phase-1 sandbox: the ground truth

Before the Waveflow component existed, the kernel was validated **standalone** as a hand-written HLS
sandbox ([`examples/rowwise_fir/sandbox/`](../../../examples/rowwise_fir/sandbox/)) — the same three
functions + DATAFLOW region, with its own csim/csynth/cosim. The sandbox is where the load-compute-store
structure was first proven on real RTL, establishing the facts the timing model relies on:

- **compute II = 1** — the `COMPUTE` loop (and the load/store burst loops) pipeline at II=1;
- **AXI bursts inferred** — the row read and row write coalesce into bursts;
- **the single `m_axi` bundle is full-duplex** — read and write use independent AR/R and AW/W channels,
  so they never contend (split-bundle buys nothing). The single-bundle top is therefore correct.

The shipped hook is that validated kernel, refactored so the interface pragmas live in the generated
top (see [the hook page](./kernel_hook.md)).

## Next

With the numbers proven equal, the remaining question is *timing*: does the loosely-timed simulation
predict the RTL's cycle counts? That is the calibration study — [extract the timing](./cosim_timing.md),
then [fit the model](./fit.md).
