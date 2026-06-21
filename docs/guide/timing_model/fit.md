---
title: Fitting a timing model
parent: Timing Models
nav_order: 5
audience: python
api: [Clock.period]
summary: "Recovering the timing-model parameters — latency, ii, unroll_factor — from a few measured (size, cycles) data points, by linear regression against the effective trip count. The cosim cycle-model work is building the infrastructure to do this against RTL ground truth."
---

# Fitting a timing model

The [block](./block.md), [streaming](./streaming.md), and [double-buffered](./double_buffered.md)
models are all driven by three numbers: **`latency`** (pipeline depth), **`ii`** (initiation
interval), and **`unroll_factor`** (elements processed per iteration). Those numbers are properties
of the *synthesized* hardware. You can read them off a Vitis HLS report by hand — but you can also
**fit** them from a handful of measured data points, so the loosely-timed model tracks real cycle
counts without you transcribing report numbers.

## The model is linear in the trip count

Recall the block compute latency:

```
cycles = latency + ii · (m − 1),      m = ceil(n / unroll_factor)
```

For a **fixed** `unroll_factor`, this is a straight line in `(m − 1)`: the slope is `ii` and the
intercept is `latency`. So if you measure the cycle count at several input sizes `n`, compute
`m − 1` for each, and fit a line `cycles = a + b·(m − 1)`, you recover:

- **`ii = b`** — the slope (cycles added per extra iteration).
- **`latency = a`** — the intercept (cost of the very first output).

Two points suffice in principle; use more and a least-squares fit so measurement noise averages
out, and check the residual / R² to confirm the model actually holds over the swept range.

## Recovering `unroll_factor`

`unroll_factor` enters through `m = ceil(n / unroll_factor)`, so it is not a free linear
coefficient — it reshapes the x-axis. Two ways to pin it down:

1. **Known from synthesis.** If you set the unroll pragma, you already know `U`; plug it in and fit
   only `latency` and `ii`.
2. **Swept.** If `U` is unknown, fit the line for each candidate `U` and pick the one with the best
   agreement (lowest residual / highest R²). The throughput asymptote also reveals it: at large
   `n`, `cycles / n → ii / unroll_factor`, so the steady-state cycles-per-element fixes the ratio.

## Where the data points come from

The measured `(n, cycles)` points are *ground truth* — and the most faithful source of ground
truth for a Waveflow design is a **Vitis HLS cosim sweep**: synthesize the kernel, run cosim at a
range of input sizes, and read the cycle count for each. That is a cycle-timed measurement (see
[LT vs CT](./models.md)) used precisely to calibrate the loosely-timed model so the fast LT
simulation predicts the slow RTL.

## Infrastructure (in progress)

Turning "run a sweep, fit `latency`/`ii`/`unroll_factor`, attach them to the component" into a
reusable, committed flow is the **cosim cycle-model** work. The pattern already exists end-to-end
in the AXI-MM command-queue example, which fits a `(depth, ii)` cycle model from a committed cosim
sweep and feeds it back into the LT simulation; the general infrastructure for arbitrary components
is being built out from there.

## See also

- [Block processing](./block.md) — the `latency + ii·(m − 1)` form this fit inverts.
- [AXI-MM Command Queue example](../../examples/mmqueue/) — a worked cosim-sweep calibration (`depth`, `ii`) that this generalizes.
- [Timing Analysis Tools — cosim timing](../timing/cosim_timing.md) — extracting cycle counts from a Vitis HLS cosim run, the measurement side of the fit.
