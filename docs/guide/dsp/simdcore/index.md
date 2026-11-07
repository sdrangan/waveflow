---
title: SIMD Core
parent: DSP Blocks
nav_order: 6
has_children: true
summary: "The SIMD core is a minimal programmable integer unit for the elementwise glue between DSP blocks: a scalar RV32IM control unit and L identical complex lanes running a subset of the RISC-V vector extension (Zve32x, LMUL 1) plus a few wf. complex and queue instructions. One vector instruction runs over a whole strip before the next starts, so a program of S instructions on N elements takes about S·N/L + D cycles on O(L) hardware, with no forwarding network. This page says what it is, why it exists beside the vector unit, what it costs, and where the ISA, the programming guide and the examples are. Status: interpreter and pysim done; HLS not yet."
---

# SIMD Core

The SIMD core (`waveflow/simdcore/`) is a small programmable unit for the **elementwise glue** between
the specialised blocks of a design: complex multiplies, gains, magnitudes, clipping, piecewise
functions, maxima and sums. FFTs, FIRs and matrix multiplies stay in their own blocks
([the SSR FFT](../ssr_fft/index.md), [the systolic array](../systolic/index.md)); the core runs what
sits between them.

It is built to be **cheap** and **familiar**:

- **Cheap.** A scalar control unit plus `L` identical lanes that never exchange data except through one
  reduction tree. Each vector instruction occupies the lanes for a whole strip of elements before the
  next starts, so dependent instructions need no forwarding network.
- **Familiar.** The instructions are RISC-V's: RV32IM for control and a subset of the vector extension
  RVV 1.0 (the `Zve32x` integer profile, `LMUL` 1), with their real encodings, so GNU as assembles the
  programs and a coding agent writes them from what it already knows. Extensions carry the `wf.` prefix
  and nothing standard is redefined.

| Page | What it covers |
|---|---|
| [Instruction set](isa.md) | every instruction, its encoding and its semantics; the `wf.` extensions; the CSRs; what is absent |
| [Programming](programming.md) | the job, windows, strip-mining, complex data, masks, reductions, tables, CG as an example |
| `examples/simdcore_mf/`, `examples/simdcore_cg/`, `examples/simdcore/` | the matched filter, CG and the toy kernels: programs, each with its golden |

## Why a second programmable unit

[The vector unit](../vecunit/index.md) chains up to `S` instructions into one pass, every slot able to
run every operation in every lane. Its calibrated resource model
([Timing and resources](../vecunit/timing.md)) has LUT terms in `S²·L` -- the operand and forwarding
multiplexers between slots -- and in the per-register fixed-point formats; at `L` 4, `S` 3 it is 82.6k
LUT (Vitis HLS 2025.1), against 21.5k for [the fixed-function CG unit](../cg_vector.md). Chaining buys
a factor of `S` in throughput, which elementwise glue, bounded by the rate its input arrives, mostly
does not need.

| | vector unit (chained, spatial) | SIMD core (temporal) |
|---|---|---|
| a program of `S` vector instructions on `N` elements | about `ceil(S / S_slots) · N/L` cycles | **`S · N/L + D`** cycles |
| resources | `S²·L` LUT, `S²·L²` FF, plus the formats | **`O(L)`** |
| at equal throughput (the core with `S·L` lanes) | `S²·L` | `S·L` |
| arithmetic | a fixed-point format per register | integers and explicit shifts |

`D` is the lane pipeline's depth, paid once when the strip is long enough (`N/L >= D`); a shorter
strip pays up to `S · D`.

## How it runs

```
for each strip of VL elements:            # vsetvli strip-mines
    for each instruction:                 # one at a time, in order
        for t in 0 .. VL/L - 1:            # pipelined, II 1
            for lane in 0 .. L-1:          # in parallel
                vd[t·L + lane] = op(vs1[...], vs2[...])
```

Element `i` of a register lives in lane `i mod L`. An instruction occupies every lane for `VL/L`
cycles; when that is at least `D`, its first result is written before the next instruction reads it.

Data arrives and leaves through **windows**: runs of vector registers that a loader fills from an
input stream and a storer drains to an output stream, each with two copies, so loading and storing
overlap the computation. A program takes the next input block with `wf.qpop` and hands an output block
over with `wf.qpush`.

## Throughput and the lane

A lane (`lane="cplx4"`, the default) has four 16-bit multipliers and a 32-bit word; a complex 16-bit
sample is one 32-bit element. With `M = 4L` multipliers:

| MACs a cycle | multiplier bound | `cplx4` lane | `real2` lane (option) |
|---|---|---|---|
| complex x complex | `M/4` | `M/4` | `M/4` |
| real x complex | `M/2` | `M/4` | `M/2` |
| real x real (`e16`, packed) | `M` | `M/2` | `M` |

`cplx4` suits complex-heavy glue; `real2` (two multipliers a 32-bit word, twice the lanes for the same
complex throughput) suits real-heavy glue.

## Status and numbers

The interpreter (the golden), the assembler, the pysim module and the examples are done
(`plans/simdcore.md`, Stages 2-4; `plans/simdcore_records.md`). The HLS bodies are not; until they are,
**every cycle count below is the interpreter's cycle model (`D` = 8), not a measurement**:

| | this core | vector unit (2025.1 RTL) |
|---|---|---|
| matched filter, 256 x 8 a frame, steady | 1,160 cycles at `L` 2 (8 DSP), 1.007x the input floor (pysim) | 1,408 (1.22x), 53 DSP |
| CG, 8 x 32, 3 iterations | 4,124 at `L` 2; 2,284 at `L` 4 | 3,447 |
| CG accuracy (residual after 3 iterations; float CG 0.0266) | 0.0282 (16-bit vectors) | 0.0639 (12-bit centre formats) |

The matched filter is bit-exact with the vector unit's golden; CG has its own integer golden.
