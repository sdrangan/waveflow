# Matrix-LT FIR accelerator (block-fidelity)

The `exec_model=hook` + `sim_fidelity=block` matrix-LT FIR from
[`plans/load_compute_store.md`](../../plans/load_compute_store.md) ("Creating a
matrix LT version" is authoritative).  One timed event per stage **per matrix**;
the validated Phase 1 sandbox kernel is reused as the hand-written DATAFLOW hook.
(The row-LT version and the dataflow sub-module codegen are **not** built here.)

```
fir_golden.py     the ONE shared bit-exact FIR golden (sandbox gen_data imports it)
fir.py            FIRAccel HwComponent: AXIMMQueue ring + 3-process block timing model
fir_dataflow.tpp  the @synthesizable hook core = the Phase 1 sandbox fir_accel kernel
fir_build.py      hand-rolled m_axi top (render_top) wrapping the hook + Vitis driver
fir_sim.py        host-driven sim: golden conformance + the overlap timeline
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
`@synthesizable(impl_file="fir_dataflow.tpp")`.  `fir_build.render_top` hand-rolls a
thin `void fir(gmem, x_off,y_off,h_off,n_rows,n_cols)` m_axi top calling the hook —
**VMAC's primary `render_top` pattern**, not the `run_proc` extractor (this `run_proc`
is 3-process timing orchestration and never executes the hook, so it is not a valid
extraction source).  The AXIMMQueue ring is sim-only; the synthesized kernel takes the
command as s_axilite scalars (exactly as VMAC bakes its command).  **No codegen-engine
extension was required.**

## What's validated (deliverables)

| Check | Result |
|---|---|
| **Golden conformance (sim)** | sim `Y` bit-exact vs `fir_golden`, single + back-to-back — PASS (`fir_sim.py`) |
| **Generated kernel csim/csynth/cosim** | bit-exact vs the shared golden (cosim 656 cyc @ 4×64) — `fir_build.py --cosim` |
| **Inter-matrix overlap (the key result)** | back-to-back: `load(N+1)` on `bus_rd` overlaps `store(N)` on `bus_wr` — 2 matrices in ~778 cyc vs ~1012 sequential |
| **Single-command timeline vs cosim** | bus-visible span comparison — see below (`fir_validate.py` → `results/timeline_single.json`) |
| **Baseline** | branch non-vitis failures == main's 15 (zero regressions) |

### Single-command timeline (4×64), sim vs RTL cosim

| span | sim (block) | cosim (RTL) | residual |
|---|---|---|---|
| read-channel (X-read) | 272 cyc (~1.03 cyc/word) | 392 cyc (~1.36 cyc/word) | 30.6% |
| write-channel (Y-write) | 230 cyc (~1.01 cyc/word) | 437 cyc (~1.92 cyc/word) | 47.4% |
| compute gap (load_end→store_begin) | 0 (hidden) | — | — |

The residuals are **expected and diagnostic** (params are PROVISIONAL): the linear
bus-transfer model misses (a) the **per-row burst setup** on the read channel (the
Phase 1 bilinear `L_row` term), and (b) the **per-row compute-coupling** of the write
channel — in RTL the Y-write span (`[2515, 6885] ns`) even *overlaps* the X-read span
(`[515, 4435] ns`), the intra-matrix full-duplex that matrix-LT deliberately abstracts.
`fir_validate.py` is the harness; the deferred per-stage calibration drives both
residuals to <eps by fitting the per-row terms into `FIRTiming`.

Run:

```bash
PYTHONPATH=. python examples/rowwise_fir/fir_sim.py          # golden conformance + overlap
PYTHONPATH=. python examples/rowwise_fir/fir_build.py --cosim  # generated kernel bit-exact
PYTHONPATH=. python examples/rowwise_fir/fir_validate.py     # sim-vs-cosim timeline
```

## Timing parameters are PROVISIONAL

`FIRTiming` is seeded from the Phase 1 bilinear fit; the bus-transfer model gives the
~1 cyc/word read/write-channel rate.  The **deferred follow-step** (noted, not done):
the per-stage cosim calibration — read-channel / write-channel / compute split read off
their own burst spans, a `≥3 n_row` × in-range `n_col` grid with an **interior**
held-out point, a back-to-back **2-matrix cosim** point to validate the overlap against
RTL, and the `sklearn.linear_model.LinearRegression` bilinear fit.  `FIRTiming` is
exactly where those numbers plug in; `fir_validate.py` is the harness that consumes them.
