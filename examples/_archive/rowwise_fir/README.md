# Matrix-LT FIR accelerator (block-fidelity)

The `exec_model=hook` + `sim_fidelity=block` matrix-LT FIR from
[`plans/load_compute_store.md`](../../plans/load_compute_store.md) ("Creating a
matrix LT version" is authoritative).  One timed event per stage **per matrix**;
the validated Phase 1 sandbox kernel is reused as the hand-written DATAFLOW hook.
(The row-LT version and the dataflow sub-module codegen are **not** built here.)

```
fir_golden.py       the ONE shared bit-exact FIR golden (sandbox gen_data imports it)
fir.py              FIRAccel HwModule: AXI-stream control (s_in/m_out) + 3-process block timing model
fir_dataflow.tpp    the @synthesizable hook core = the Phase 1 sandbox fir_accel kernel
fir_top.cpp         static m_axi + AXI-stream-control top (copied into gen/fir.cpp); + fir.hpp / fir_tb.cpp / run.tcl
fir_respond_impl.tpp hand-written response hook (m_out), like shared_mem's hist_respond_impl
fir_build.py        copies the static top/hpp/tb/tcl into gen/ + the Vitis driver
fir_sim.py          host-driven sim: golden conformance + the overlap timeline
fir_validate.py   sim-vs-cosim bus-visible timeline comparison (single command)
gen/              generated kernel sources (fir.cpp/.hpp/.tpp + TB + run.tcl)
sandbox/          Phase 1 hand-written HLS sandbox (the kernel reused here)
results/          committed artifacts
```

## What it is

Three persistent stage processes — `load` / `compute` / `store` — over **per-direction**
bus resources (`bus_rd` for AR/R, `bus_wr` for AW/W).  A single `m_axi` bundle is
full-duplex (the Phase 1 correction), so:

- `compute` runs from BRAM and holds neither channel → overlaps `load` and `store`.
- `load(N+1)` (bus_rd) and `store(N)` (bus_wr) use **different** channels → they
  overlap too.  Per-matrix throughput = `max(read-channel, write-channel, compute)`.
- There is **no single-port II=2 floor** for a read+write kernel.

A single `run_proc` would instead serialize matrices and idle the bus during compute;
the three-process structure is what models the inter-matrix pipelining.

Inter-stage handoffs are fictitious (unsynthesized) `FIRCompMsg` / `FIRStoreMsg`
dataclasses carrying the data + an absolute-time `tstart` (the pipeline-fill quantity).
Stage timing is driven by the bus-transfer durations (`read_slice`/`write_slice`, ~1
cycle/word — FIR's memory-bound rate) plus a compute `timeout`; the data itself moves
on a near-zero-latency memory so there is no double-count.

## Codegen (`exec_model=hook`)

The synthesizable unit is the **whole** load-compute-store DATAFLOW kernel
(`fir_dataflow.tpp` = the validated Phase 1 `fir_accel`), bound via
`@synthesizable(impl_file="fir_dataflow.tpp")`.  The top is a **static source file**
(`fir_top.cpp`, copied verbatim into `gen/fir.cpp` — no templating) that adds
**AXI-stream control** (the command on `s_in`, the response on `m_out` via
`fir_respond_impl.tpp`, like `shared_mem`) around a `void fir(s_in, m_out, gmem)` m_axi
top calling the hook.  The `run_proc` extractor is **not** used (this `run_proc` is
3-process timing orchestration and never executes the hook, so it is not a valid
extraction source).  FIR is the first example with static-file codegen.  **No
codegen-engine extension was required.**

## What's validated (deliverables)

| Check | Result |
|---|---|
| **Golden conformance (sim)** | sim `Y` bit-exact vs `fir_golden`, single + back-to-back — PASS (`fir_sim.py`) |
| **Generated kernel csim/csynth/cosim** | bit-exact vs the shared golden (cosim 656 cyc @ 4×64) — `fir_build.py --cosim` |
| **Inter-matrix overlap (the key result)** | back-to-back: `load(N+1)` on `bus_rd` overlaps `store(N)` on `bus_wr` — 2 matrices in ~778 cyc vs ~1012 sequential |
| **Latency fix (early-anchored Y-write)** | single-command whole-kernel tracks RTL (656, not the 829 serial estimate); the Y-write overlaps the X-read (`fir_validate.py`) |
| **Physical timing model** | deterministic occupancy (beats==nwords) + II=1 compute + calibrated `g(n_col)`; **Gate 1 (2,256) 0.11%, Gate 2 untrained-n_col 0.14% / 0.60%**, sim vs cosim (`fir_calibrate.py`) |
| **Whole-grid sim reconstruction** | ≤1.30% (worst at the smallest matrix; the rest < 0.3%) |
| **Baseline** | branch non-vitis failures == main's 15 (zero regressions) |

### Timing model (physical, near-fit-free)

The whole-kernel decomposes — and the sim composes — as
`whole = (n_col+T) + trips + n_row·g(n_col) + fill_const = fill + max(write_occ, compute_body)`.
**Channel occupancy is deterministic** (each transfer beat is one word ⇒ `nwords +
setup·num_trans`, the `BusTiming`; *not* fitted), **compute is II=1** (`trips + (n_row−1)·g`,
slope 1 exact), and the **only calibrated term is `g(n_col)`** — the per-row pipeline /
ping-pong depth, a *saturating* `InterpCalibModel` lookup (not a `sqrt` fudge).  The store
finishes **under compute's shadow** (`min_span = compute_body` on the Y-write).  The old
`write_span` had conflated the compute-stall into a fitted span — that was the entire apparent
curvature.  Full decomposition, the sanity check, `g`, and the gates:
**`results/fir_calibration_results.md`**.

Run:

```bash
PYTHONPATH=. python examples/rowwise_fir/fir_sim.py            # golden conformance + overlap
PYTHONPATH=. python examples/rowwise_fir/fir_build.py --cosim  # generated kernel bit-exact
PYTHONPATH=. python examples/rowwise_fir/fir_calibrate.py --measure   # cosim grid (slow)
PYTHONPATH=. python examples/rowwise_fir/fir_calibrate.py --fit       # fit + validate
```

## Calibration status

Both single-command gates meet target on the **actual sim**: Gate 1 (interior holdout 2,256)
**0.11%**, Gate 2 (untrained n_col 4×128 / 4×512) **0.14% / 0.60%**.  The model is physical and
near-fit-free: occupancy and the II=1 compute are exact (fit-free); the sole calibrated term is
the per-row pipeline depth `g(n_col)`, which saturates (so a few columns interpolate cleanly).
The one residual is `g(n_col)` itself — a *per-row* quantity that block granularity can only see
as `n_row·g(n_col)`; modeling it once per row is the fit-free lift a **row-LT** version would get
(`results/fir_calibration_results.md`).  The back-to-back / inter-command throughput claim still
awaits a multi-command kernel cosim (deferred).
