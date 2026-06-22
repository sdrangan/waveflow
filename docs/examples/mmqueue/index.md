---
title: AXI-MM Command Queue (VMAC)
parent: Examples
nav_order: 6
has_children: true
---

# AXI-MM Command Queue (VMAC)

**VMAC** is a complex vector-MAC accelerator: it computes element-wise on complex
fixed-point matrices and optionally reduces over rows. It supports **three
element-wise operations plus a reduce** — `scalar_mult` (`α·A`), `inner_prod`
(`A·conj(B)`), and `sum` (`A + B`), each with an optional per-column row sum. A host
drives it not over a control stream but through a **command queue in shared memory**:
the host appends `VmacCmd`s to a ring buffer, and a free-running accelerator dequeues
and executes them over a single `m_axi` master. This is the
[command-queue interface](../../guide/interface/mmqueue.md) — control moved off the
stream and into memory — made concrete.

## The system

One host, one accelerator, one shared memory, over a single `m_axi` master:

```
   host ──AXI-MM command queue──▶ VMAC ──m_axi (gmem)──▶ shared memory
 (vmac_host.py)                  (vmac.py)                A | B | Y regions
```

The host enqueues commands into the ring; VMAC dequeues them, reads its operand
matrices `A` (and `B`) from memory over `m_axi`, computes, and writes the result `Y`
back to the same bundle. The ring's `head`/`tail` pointers live in that memory too,
so command flow and data flow share one interconnect.

## Why this is the capstone example

It is the last and most complete example in the
[examples progression](../), and it is distinctive on two counts:

- It is the only example that is **also a timing study** — it does not just ask
  "are the numbers right" but "does the loosely-timed simulation predict the right
  *timing*, and can a Vitis cosim calibrate it." That study is the [timing](./timing.md)
  page.
- It is the canonical **one-golden accelerator anatomy** — a single author-written
  golden (`execute`) that both the SimPy model and the synthesizable kernel are
  checked against. The [Python model](./pymodel.md) page is the reference other tiles
  copy.

## Walkthrough

1. [What we're building](./vmac.md) — the host-driven complex correlation the example
   computes, the three-op + reduce model, and the system at a glance.
2. [Data types — the command and formats](./datatypes.md) — `VmacCmd`, the operand
   region descriptors, and `VmacFormats`, the dependent types derived from a command.
3. [Fixed-point arithmetic](./arithmetic.md) — the operand / accumulator / output
   number formats, how precision grows through the datapath, and the single
   requantize.
4. [The Python model](./pymodel.md) — the one author-written golden `execute`, the
   synthesizable `vmac_compute` shell, and element-indexed `Region` access.
5. [Python simulation](./pysim.md) — the SimPy queue sim: host + VMAC over a shared
   memory, the free-running consumer loop, and parity against the golden.
6. [Code generation](./codegen.md) — the auto-extracted, `m_axi`-only synthesizable
   top that dequeues from the ring and runs `vmac_compute`.
7. [Writing the kernel hook](./hook.md) — the hand-written `vmac_compute_impl.tpp`
   complex datapath.
8. [C and RTL simulation](./rtlsim.md) — Vitis csim/cosim against the one golden, the
   conformance matrix, and throughput vs packing factor.
9. [Timing — the LT model + cosim calibration](./timing.md) — the capstone timing
   study (`ab_eq` bus-vs-latency, naive → calibrated → RTL, the committed figure).
