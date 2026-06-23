---
title: Examples
parent: Waveflow
nav_order: 3
has_children: true
---
# Examples

Waveflow's examples are a **teaching progression**, and they come in **two
families**. You learn to *represent and compute on data* first, then how to *move
that data between modules* over real hardware protocols:

1. **Data & schema patterns** — how a design's numbers are typed, stored, and
   computed on, kept **vectorized** (numpy arrays end to end) so functional
   simulation is fast *and* bit-exact. Start here.
2. **Interface patterns** — how a host and an accelerator exchange that data over
   the AXI-* protocols, one new interface concept per example.

The rationale is bottom-up: the interface examples *carry* schema-typed data across
their ports, so it pays to understand the data layer before the protocols that
transport it. Every directory is named for the **pattern** it teaches; the
computation keeps its own identity in the files inside (e.g. `stream_inband/` holds
the polynomial accelerator in `poly.py`).

## Family 1 — data & schema patterns

These teach the [data schema](../guide/schema/) and
[vectorization](../guide/vectorization/) layers: typed fields, numpy-backed arrays,
and the type-preserving operators, all proven **bit-exact** against Vitis.

| Example                    | Vehicle               | What it teaches                                                                                                                 | Status           |
| -------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| [`basic_vec`](./basic_vec/) | one MAC (`a*b + c`) | vectorized golden vs vectorized Vitis, bit-exact for int / float / fixed — the[vectorization](../guide/vectorization/) front-door | available        |
| `schemas/fixedpoint`     | edge-value sweep      | the rigorous fixed-point conformance harness (all `QMode`/`OMode` × widths) behind [FixedField](../guide/schema/python/fixpoint.md)  | available (code) |

`basic_vec` is the teaching front-door (minimal, readable); `schemas/fixedpoint` is
the exhaustive proof. They share the same conformance machinery.

## Family 2 — interface patterns

These move more of the host↔accelerator contract off the control plane and into
shared structures at each step, one new interface concept at a time. They are the
full five-stage flow below.

## The general Waveflow flow

Python is the single source of truth. Every *full* example walks the same five
stages, each derived from the one before:

1. **Python model** — the golden numerical behavior, in numpy / PyTorch.
2. **Python simulation** — the SimPy transactional simulation (Components +
   Interfaces): protocol-accurate, cycle-approximate.
3. **Code generation** — a Vitis HLS C++ kernel **and** testbench emitted from
   that same Python model.
4. **C and RTL simulation** — Vitis C-simulation, then C-synthesis followed by
   RTL co-simulation, each checked against the Python golden model.
5. **Timing extraction** — cycle and burst measurements pulled from
   co-simulation and fed back into the Python timing model.

The *Flow coverage* column below cites these stage numbers.

## The five interface patterns

The progression moves more of the host↔accelerator contract off the control
plane and into shared structures at each step: from **all control in registers**,
to **control in-band on the data stream**, to **data in memory**, to **control
also in memory**. `pure_stream` slots in early as the boundary-free streaming
base case.

| # | Pattern (directory)                | Vehicle               | New concept introduced                                         | Flow coverage             | Status                                 |
| - | ---------------------------------- | --------------------- | -------------------------------------------------------------- | ------------------------- | -------------------------------------- |
| 1 | [`regmap`](./regmap/)               | simple function       | register-mapped control (AXI4-Lite)                            | stages 1–5               | available                              |
| 2 | `pure_stream`                    | moving-average filter | streaming dataflow — no packet boundary, no TLAST, no control | planned                   | reserved (not built yet)               |
| 3 | [`stream_inband`](./stream_inband/) | polynomial            | packetization (TLAST) + in-band control on the stream          | stages 1–5               | available                              |
| 4 | [`shared_mem`](./shared_mem/)       | histogram             | data in memory (AXI-MM), control over a dedicated stream       | stages 1–5               | available; codegen upgrade in progress |
| 5 | [`mmqueue`](./mmqueue/)             | complex vector MAC (VMAC) | control *also* in memory, via a descriptor queue            | stages 1–5               | available                              |
| 6 | [`rowwise_fir`](./rowwise_fir/)     | per-row FIR filter    | turns *inward*: the internal **load-compute-store dataflow** + a cosim-calibrated timing model | stages 1–5 + calibration | available |

The shared-memory (`shared_mem`) example is the reference for AXI-MM (`m_axi`)
codegen — multiple buffers and element types read/written over one bundle, with
the kernel and testbench generated from the Python component.

**[`rowwise_fir`](./rowwise_fir/) is different in kind.** Where #1–5 each move more of
the *interface* contract into shared structures, rowwise FIR **reuses** the mm-queue
interface and instead studies the accelerator's *internal* structure — the
load-compute-store dataflow — and how to give it a **physical, cosim-calibrated timing
model**. It is the capstone of the [timing-model](../guide/timing_model/) and
[calibration](../guide/calib/) arc.
