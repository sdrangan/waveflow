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
| **Latency fix (early-anchored Y-write)** | single-command whole-kernel tracks RTL (656, not the 829 serial estimate); the Y-write overlaps the X-read (`fir_validate.py`) |
| **Per-stage calibration** | sklearn bilinear (+concave `sqrt(n_col)`) fit from a `{1,2,4,8}×{64,256,1024}` cosim grid; **interior held-out (2,256) whole-kernel 0.02%** (`fir_calibrate.py`) |
| **Back-to-back vs cosim** | sim 2×(n_row) vs `cosim(2·n_row)`: 6–10% (converging with size) |
| **Baseline** | branch non-vitis failures == main's 15 (zero regressions) |

### Timing model (calibrated)

`FIRTiming` holds per-span fitted models (`results/fir_calibration.json`): the X-read
span (bilinear), the Y-write span and the first-Y-row `fill` (bilinear + a concave
`sqrt(n_col)` term, because the per-row write gap and compute fill **saturate in n_col**).
The store is **early-anchored** (Y-write begins at the first-Y-row time → overlaps the
X-read: the latency fix), and successive writes serialize by their effective spans
(back-to-back throughput).  Full numbers, held-out residuals (interior + untrained-n_col),
back-to-back, and the ship-gate verdict: **`results/fir_calibration_results.md`**.

Run:

```bash
PYTHONPATH=. python examples/rowwise_fir/fir_sim.py            # golden conformance + overlap
PYTHONPATH=. python examples/rowwise_fir/fir_build.py --cosim  # generated kernel bit-exact
PYTHONPATH=. python examples/rowwise_fir/fir_calibrate.py --measure   # cosim grid (slow)
PYTHONPATH=. python examples/rowwise_fir/fir_calibrate.py --fit       # fit + validate
```

## Calibration status

The interior held-out (the plan's verdict) meets target (whole-kernel 0.02%, per-event
≤3.5%).  Honest caveats in `results/fir_calibration_results.md`: untrained-n_col
generalization is 6–10% (the 3-column grid undersamples the concave n_col curve — denser
columns would close it), and the back-to-back gate uses `cosim(2·n_row continuous)` as a
proxy because the definitive free-running reference (the AXIMMQueue ring kernel) is the
deferred codegen.
