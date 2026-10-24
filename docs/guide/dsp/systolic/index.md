---
title: Systolic Matrix Multiply
parent: DSP Blocks
nav_order: 3
has_children: true
summary: "Waveflow's systolic matrix multiply: C = q(A·B) or q(Aᴴ·B) on complex fixed-point matrices, exactly before one rounding, on an output-stationary array of R × C processing elements. Two modules: SystolicEngine, streamed, for products that fit on chip, with panels for a long inner dimension and operand reuse; SystolicEngineMem, the engine behind memory for matrices larger than it. This page says which to use and where each is described: how a systolic array works and what bounds it, the engine, and the engine behind memory."
---

# Systolic Matrix Multiply

Waveflow currently offers two modules to multiply complex fixed-point matrices: `SystolicEngine` and
`SystolicEngineMem`. Both compute a complex fixed-point matrix product `C = q(A·B)` or `C = q(Aᴴ·B)`,
where every product and every sum in `A·B` or `Aᴴ·B` is computed exactly (wide enough that nothing
overflows or rounds), and `q(·)` rounds and saturates the result **once**, to `C`'s format. The
computation is bit for bit like the model `waveflow.linalg.matmul.matmul`. Both share one array --
output-stationary, `R × C` processing elements.

The key difference is where the operands live:

| | `SystolicEngine` | `SystolicEngineMem` |
|---|---|---|
| interface | streams: a command, then `A` and `B`, on an input stream; a `Resp` and `C` on an output stream | memory: a job descriptor on an input stream, with the addresses of `A`, `B` and `C`; `A` and `B` read and `C` written over `m_axi`; an `EngineDone` out on a stream |
| largest product | `Mmax × Pmax·Kmax × Nmax`, held on chip: a long inner dimension is split into *panels*, summed on chip | any `M`, `N` that are multiples of `Mmax`, `Nmax`, split into *tiles* of `C`, one engine command each; `K ≤ Pmax·Kmax`, in the engine's panels |
| reuse | `keep_a`: one `A` for several `B` | none across tiles |
| at the centre | `8×8×32` back to back: 171 cycles a product (64-bit words), 94 (128-bit) | `32×32×64`: done at 5,813 cycles (64-bit), 3,033 (128-bit) |

(Vitis HLS / Vivado xsim 2025.1, `xczu48dr`, 4 ns, the centre configuration of
[the engine page](systolic_engine.md#parameters).)

## The pages

- [How a systolic array works](systolic_theory.md) -- a product that fits on the array: the dimensions,
  a tile, the skew, the tile period, and the three bounds on its time (compute, input, output) and its
  memory.
- [Systolic engine](systolic_engine.md) -- `SystolicEngine`: what it computes, its parameters, the
  command, panels for a long inner dimension and how they are iterated, its architecture (banked buffers
  read in place, a continuous array, the collector), performance, limits.
- [Systolic engine behind memory](systolic_engine_mem.md) -- `SystolicEngineMem`: the job, how a large
  product is divided into tiles and panels and the loop over them, the block-major layout, the feeder,
  performance, limits.

The older `SystolicUnit` (`waveflow/linalg/systolic.py`), which the engine supersedes, remains only for
`examples/mimo_cg` until that example moves to the engine; it is no longer documented here.
