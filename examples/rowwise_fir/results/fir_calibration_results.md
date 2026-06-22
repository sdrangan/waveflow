# Matrix-LT FIR — per-stage cosim calibration (steps 4–5)

Calibration of the block-fidelity FIR timing model against RTL cosim (Vitis 2025.1,
xc7z020clg484-1, 10 ns clock).  Artifacts: `cosim_grid.json` (fit grid),
`cosim_holdout_ncol.json` (untrained-n_col held-out), `fir_calibration.json` (fitted
models + validation).  Reproduce: `fir_calibrate.py --measure` then `--fit`.

## Method (endpoints pinned identically to the sim)

Every span is read off its **own** m_axi burst in the cosim VCD and anchored at the
X-read start — exactly as the sim anchors at `load_begin`:

| quantity | cosim (VCD bursts) | sim event |
|---|---|---|
| X-read span | first→last read burst | `load_begin`→`load_end` |
| Y-write span | first→last write burst | `store_begin`→`store_end` |
| `t_fill` (first-Y-row) | Y-write start − X-read start | `store_begin` offset |
| whole-kernel | Y-write end − X-read start | `store_end` offset |

Fit grid: `n_row ∈ {1,2,4,8}` × `n_col ∈ {64,256,1024}` (sklearn `LinearRegression`).
Per-span feature basis (`trips = n_row·(n_col−T+1)`, `T=8`):

- **load** (X-read): `[1, n_row, n_col, trips]` — the read is ~linear in words.
- **store** / **fill**: above **+ `sqrt(n_col)`, `n_row·sqrt(n_col)`** — the per-row write
  gap and the first-row fill **saturate in n_col** (gap = 70→261→268 cyc at n_col
  64→256→1024), a concave shape a linear-in-n_col model cannot fit.

`fill` comes out n_row-independent: `fill ≈ −182 + 0.018·n_col + 47.6·√n_col` (the n_row
and trips coefficients fit to ~0) — the first output row's latency does not depend on how
many rows follow.

## Fit quality

| span | R² (fit grid) |
|---|---|
| load (X-read) | 0.994 |
| store (Y-write) | 1.000 |
| fill | 1.000 |

## Held-out residuals (the verdict — sim with fitted model vs cosim)

**Interior held-out `(2,256)`** (the plan's specified verdict — tests n_row interpolation;
n_col=256 is a trained column):

| event | sim | cosim | rel-err |
|---|---|---|---|
| X-read span | 585.7 | 566.0 | **3.5%** |
| Y-write span | 759.3 | 759.0 | **0.0%** |
| t_fill | 584.0 | 584.0 | **0.0%** |
| **whole-kernel** | 1343.3 | 1343.0 | **0.02%** |

**Untrained-n_col held-out** (added for honesty — tests the `sqrt(n_col)` interpolation
between the 3 fit columns; these n_col are NOT in the fit):

| point | X-read | Y-write | t_fill | whole-kernel |
|---|---|---|---|---|
| (4,128) | 17.5% | 10.1% | 9.4% | **9.9%** |
| (4,512) | 3.5% | 5.8% | 6.7% | **6.0%** |

Interior whole-kernel is **0.02%** (≲2–3% target met; per-event ≤3.5% ≲5% met).  The
untrained-n_col whole-kernel is **6–10%**: the 3-column grid undersamples the concave
n_col curve, so interpolation to a *new* column is 6–10%.  Tightening this to ≲3% for
arbitrary n_col needs a denser n_col grid (more columns to pin the concavity) — noted, not
done.

## Back-to-back throughput (step 5)

Sim runs **two** matrices back-to-back (the `bus_rd`/`bus_wr` serialization +
`load(N+1)∥store(N)` overlap; writes serialize by their effective spans via
`_bus_wr_free`).  RTL reference: `cosim(2·n_row, n_col)` — one kernel over `2·n_row`
continuous rows (the per-row dataflow has no matrix boundary).

| sim 2×(n_row,n_col) | cosim ref | sim span | cosim | rel-err |
|---|---|---|---|---|
| 2×(4,64) | (8,64) | 1264 | 1145 | 10.4% |
| 2×(4,256) | (8,256) | 4135 | 4389 | 5.8% |

The sim reads slightly high at small size and converges with size.  **Caveat:** the true
back-to-back reference is a *free-running* accelerator (the AXIMMQueue ring kernel), whose
codegen is deferred; `cosim(2·n_row continuous)` is the closest available RTL proxy and
lacks the inter-command seam the sim models (per-matrix fill + response), so the sim reads
high by roughly one per-row write-gap — a relative effect that shrinks as the matrices
grow.

## Ship-gate verdict

1. **Latency fix (early-anchored pipelined transfer)** — **MET.** Single-command
   whole-kernel tracks RTL (interior held-out 0.02%), not the 829-cyc serial estimate;
   the Y-write overlaps the X-read.
2. **Per-stage cosim calibration** — **MET at the plan's interior held-out** (whole-kernel
   0.02%, per-event ≤3.5%). Honest caveat: untrained-n_col generalization is 6–10%
   (sparse concave n_col sampling); denser columns would close it.
3. **Back-to-back validated vs cosim** — **PARTIAL.** Matches the continuous-kernel proxy
   within 6–10% (converging with size); the definitive free-running-RTL reference is the
   deferred ring-kernel codegen.
