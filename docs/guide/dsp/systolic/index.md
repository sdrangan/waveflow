---
title: Systolic Matrix Multiply
parent: DSP Blocks
nav_order: 3
has_children: true
summary: "Waveflow's systolic matrix multiply: C = q(A·B) or q(Aᴴ·B) on complex fixed-point matrices, exactly before one rounding, on an output-stationary array of R × C processing elements. Two modules: SystolicEngine, streamed, for products that fit on chip, with panels for a long inner dimension and operand reuse; SystolicEngineMem, the engine behind memory for matrices larger than it. This page says which to use and where each is described: how a systolic array works and what bounds it, the engine, and the engine behind memory."
---

# Systolic Matrix Multiply

Two modules multiply complex fixed-point matrices, `C = q(A·B)` or `C = q(Aᴴ·B)`, every product and
sum exact and `C` rounded **once**, bit for bit like the model `waveflow.linalg.matmul.matmul`. They
share one array -- output-stationary, `R × C` processing elements -- and differ in where the operands
live:

| | `SystolicEngine` | `SystolicEngineMem` |
|---|---|---|
| operands and results | on streams: a command and its operands in; a `Resp` and `C` out | in memory: a job descriptor in, `C` written back, an `EngineDone` out |
| largest product | `Mmax × Pmax·Kmax × Nmax` | any multiple of `Mmax`, `Nmax`; `K ≤ Pmax·Kmax` |
| reuse | `keep_a`: one `A` for several `B` | none across tiles |
| at the centre | `8×8×32` back to back: 171 cycles a product (64-bit words), 94 (128-bit) | `32×32×64`: done at 5,813 cycles (64-bit), 3,033 (128-bit) |

(Vitis HLS / Vivado xsim 2025.1, `xczu48dr`, 4 ns, the centre configuration of
[the engine page](systolic_engine.md#parameters).)

## The pages

- [How a systolic array works](systolic_theory.md) -- one panel on the array: the dimensions, a tile,
  the skew, the tile period, and the three bounds on its time (compute, input, output) and its memory.
- [Systolic engine](systolic_engine.md) -- `SystolicEngine`: what it computes, its parameters, the
  command, its architecture (banked buffers read in place, a continuous array, the collector), what
  the RTL measured, testing, limits.
- [Systolic engine behind memory](systolic_engine_mem.md) -- `SystolicEngineMem`: the job, the
  block-major layout, the outer loop and its feeder, what the RTL measured, testing, limits.

The older `SystolicUnit` (`waveflow/linalg/systolic.py`), which the engine supersedes, remains only for
`examples/mimo_cg` until that example moves to the engine; it is no longer documented here.
