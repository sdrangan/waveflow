---
title: DSP Blocks
parent: Guide
nav_order: 10.8
has_children: true
audience: python
api: [SsrFft, SystolicEngine, SystolicEngineMem, CgVectorUnit, CgVectorCore, VecUnit]
summary: "Waveflow's own DSP blocks, written as free-running Waveflow modules rather than wrapped vendor IP: the full-rate SSR FFT, bit-exact with AMD's Vitis L1 FFT and ten times its throughput as shipped, and the complex fixed-point linear-algebra components -- the systolic matrix multiply, the CG vector unit and the programmable vector unit -- with the conventions they share."
---

# DSP Blocks

[Vitis L1 Blocks](../vitis_l1/index.md) wraps AMD's library blocks as they ship. This section is the
other route: blocks written in Waveflow -- a bit-exact Python model, one free-running `hls::task` per
piece of hardware, a composite module that generates its own top -- which take what is good from a
vendor library and rebuild what is not.

- [The full-rate SSR FFT](ssr_fft/index.md) -- `SsrFft`: the Vitis FFT's arithmetic, bit for bit, at a new
  frame every `L/R` cycles.
- [Linear-Algebra Conventions](linalg_common.md) -- what the linear-algebra components share: formats,
  lane groups, the message header and its statuses, the build step, the cost data.
- [Systolic Matrix Multiply](systolic/index.md) -- `C = q(A·B)` and `C = q(Aᴴ·B)` on complex
  fixed-point matrices: [the systolic engine](systolic/systolic_engine.md), streamed, with panels and
  operand reuse, and [the engine behind memory](systolic/systolic_engine_mem.md) for larger matrices.
  Starts from [how a systolic array works](systolic/systolic_theory.md).
- [CG Vector Unit](cg_vector.md) -- `CgVectorCore` and `CgVectorUnit`: the vector steps of conjugate
  gradient on `A X = B`, multi-RHS.
- [Vector Unit](vecunit.md) -- `VecUnit`: a programmable complex fixed-point vector unit, RVV-style,
  with a format per register and decoupled load, execute and store tasks; CG and a matched filter
  run on it as programs.
