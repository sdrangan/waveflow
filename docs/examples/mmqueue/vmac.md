---
title: What we're building
parent: AXI-MM Command Queue (VMAC)
nav_order: 1
has_children: false
---

# What we're building

VMAC is a **complex vector-MAC accelerator**: a small ALU over complex fixed-point
matrices, driven by commands the host leaves in shared memory. This page sets up
*what* it computes and *how* the host talks to it; the later pages build each layer.

## The motivating computation

The worked scenario is a **host-driven complex correlation**. The host has two
complex matrices `A` and `B` in memory and wants a normalized correlation
`rho = abcorr / anorm`, where

- `anorm` is the auto-correlation energy `A · conj(A)`, reduced to one row, and
- `abcorr` is the cross-correlation `A · conj(B)`, reduced to one row.

The split between hardware and host is deliberate:

1. The host writes `A` and `B` into their memory regions.
2. It **enqueues two commands** — both `inner_prod` with `reduce` set: one with
   `B` aimed at `A` (giving `anorm`), one with `B` aimed at `B` (giving `abcorr`) —
   then an `end` sentinel.
3. VMAC dequeues each command, reads the operands over `m_axi`, computes the reduced
   inner product, and writes the result `Y` back to memory.
4. The host reads both `Y` regions back and computes the **division**
   `rho = abcorr / anorm` itself.

The division stays on the host on purpose: it is the one step that is awkward to
synthesize (a complex/real reciprocal), so it is an explicit **hardware/host split**
— the accelerator does the bandwidth-heavy multiply-accumulate, the host does the
single scalar divide. This is modeled in
[`examples/vmac/vmac_host.py`](../../../examples/vmac/vmac_host.py).

## The operation set

VMAC is not single-purpose; the correlation above uses one of its **three
element-wise operations plus an optional reduce**. Presented as a general complex
vector ALU:

| `op` | computes (per element `i,j`) | second operand | scalar |
| --- | --- | --- | --- |
| `scalar_mult` | `α[i] · A[i,j]` | — | `α` (immediate or per-row) |
| `inner_prod`  | `A[i,j] · conj(B[i,j])` | `B` | — |
| `sum`         | `A[i,j] + B[i,j]` | `B` | — |

The `reduce` flag, when set, sums each result down its rows to a single output row
(`Y[j] = Σ_i R[i,j]`). A fourth opcode, `end`, carries no computation — it is the
**sentinel** that breaks the accelerator's run loop. The correlation example uses
`inner_prod + reduce`; the conformance suite exercises all three ops with and
without reduce. The opcodes and the command that carries them are the subject of the
[data types](./datatypes.md) page.

## The system

One host, one accelerator, one shared memory, over a single `m_axi` master:

```
   host ──AXI-MM command queue──▶ VMAC ──m_axi (gmem)──▶ shared memory
 (vmac_host.py)                  (vmac.py)                A | B | Y regions
```

What makes VMAC distinctive among the [examples](../) is that the host does **not**
push commands down a dedicated control stream. It appends them to a **ring buffer in
the same shared memory** the operands live in, and a free-running accelerator
dequeues them. That ring is the
[AXI-MM command queue](../../guide/interface/mmqueue.md) interface; here it carries
`VmacCmd`s. Control and data share one interconnect.

## Roadmap

The remaining pages build VMAC bottom-up:

- [Data types](./datatypes.md) and [arithmetic](./arithmetic.md) — the command, its
  element/format types, and the fixed-point datapath.
- [The Python model](./pymodel.md) and [Python simulation](./pysim.md) — the one
  author-written golden, the synthesizable shell, and the SimPy queue sim.
- [Code generation](./codegen.md) and [the kernel hook](./hook.md) — the generated
  synthesizable top and the hand-written complex datapath.
- [C and RTL simulation](./rtlsim.md) and [timing](./timing.md) — Vitis conformance
  and the loosely-timed-model calibration that is this example's headline.
