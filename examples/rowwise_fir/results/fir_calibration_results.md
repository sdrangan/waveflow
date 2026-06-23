# Matrix-LT FIR — physical, near-fit-free timing model

Calibration of the block-fidelity FIR timing model against RTL cosim (Vitis 2025.1,
xc7z020clg484-1, 10 ns clock).  Artifacts: `cosim_grid.json` (fit grid),
`cosim_holdout_ncol.json` (untrained-n_col held-out), `fir_calibration.json` (the calibrated
`g` table + constants + the sim-measured gates).  Reproduce: `fir_calibrate.py --measure` then
`--fit` (the `--fit` step needs no Vitis — it reads the committed cosim grid).

## The model — what each piece physically is

The whole-kernel **decomposes**, and the sim **composes**, as

```
whole = (n_col + T) + trips + n_row·g(n_col) + fill_const          (master equation)
      = fill + max(write_occ, compute_body)                        (the sim composition)
```

with `trips = n_row·(n_col − T + 1)`, `T = 8`.  Every term is physical:

| piece | form | fitted? |
|---|---|---|
| **channel occupancy** | `nwords + setup·num_trans` (slope **1**, each transfer beat = one word) | **no** — deterministic |
| **compute body** (II=1) | `trips + (n_row−1)·g(n_col)` (one output / cycle) | **no** — slope 1 exact |
| **`fill`** (ramp to 1st output) | `(n_col+T) + g(n_col) + fill_const` | `fill_const` = one scalar |
| **`g(n_col)`** | per-row pipeline / 2-buffer ping-pong depth | **yes** — the *only* calibrated curve |

`g(n_col)` appears **`n_row` times**: once in `fill`, `(n_row−1)` times as inter-row gaps in
the compute body.

### The realization (why the old model had "curvature")

The earlier `write_span` was the **wall-clock data-phase span** — it conflated the *channel
occupancy* with the *compute-stall* (the store idling on the write data channel, `TVALID=0`,
while compute caught up).  That stall is **compute**, already a sim primitive; folding it into a
fitted span term is what produced the apparent `sqrt(n_col)` curvature.  Separating the two —
deterministic occupancy + an II=1 compute that the sim lets pace the store — removes it.  The
store now finishes **under compute's shadow**: its bus-visible span is `max(write_occ,
compute_body)` (the `min_span` of `Region.write_slice_pipelined`), so when compute is the
bottleneck the channel hides under it.

## Sanity check — channel occupancy is deterministic (transfer beats == nwords)

Summing only `beat_type == TRANSFER` per direction (`waveflow/utils/vcd.py`
`AximmBeatType`; `IDLE`/`STALL` dropped) over all 12 grid points:

- **read** beats == `n_row·(n_col + T)` — exact, all 12 (the kernel re-reads the `T` taps per row).
- **write** beats == `trips = n_row·(n_col − T + 1)` — exact, all 12.

So occupancy is `nwords + setup·num_trans` with slope **1** and `setup` a small fixed per-burst
address latency (`setup = 2`; immaterial while compute-bound — it only gates the write-bound
regime).  *Not* a fitted curve.

## Compute is II=1 — exact

Fitting `compute_body` against `trips` on the **single-row** points (no inter-row boundary):
slope **1.000**, intercept **−1.0**, **R² = 1.0000**.  The steady MAC throughput is exactly one
output per cycle.  The only non-trivial part is the per-row refill `g`.

## `g(n_col)` — the one calibrated term (a measured, saturating lookup)

Measured from the inter-row gaps, `g = (y_write_span − trips)/(n_row − 1)` (constant across
`n_row` for each `n_col`):

| n_col | 64 | 256 | 1024 |
|---|---|---|---|
| `g(n_col)` (cyc) | 69.5 | 260.3 | 268.5 |

`g` **saturates** (~268, the FIR pipeline depth), so it is carried as a calibrated
`InterpCalibModel` *lookup* (linear interpolation between samples, flat beyond) — **not** a
`sqrt` basis.  Saturation makes it cheap: three columns already interpolate the untrained
columns to <1% (denser sampling is a trivial, additive future tightening — no model change).

## Gates — sim (loading the calibration) vs cosim, on the **actual** simulation

| gate | point | sim whole | cosim whole | rel-err |
|---|---|---|---|---|
| **Gate 1** single-command (n_row held out; g(256) trained) | (2,256) | 1341.5 | 1343 | **0.11%** |
| **Gate 2** untrained n_col (g interpolated) | (4,128) | 1211.3 | 1213 | **0.14%** |
| **Gate 2** untrained n_col (g interpolated) | (4,512) | 3651.0 | 3673 | **0.60%** |

Whole-grid sim reconstruction: **max 1.30%** (worst at the smallest matrix (1,64), where the
fixed `fill_const` dominates a 256-cycle kernel; every other point < 0.3%).

> Composition caveat (the quiet failure the model had to clear): the early-anchored Y-write
> must do its **functional** movement *anchored* at `t_out_start`, otherwise — when the store
> runs late behind a long read — the write restarts at `now` and overruns the span (seen as an
> 11% miss at (4,512) before the fix).  `Region.write_slice_pipelined` now anchors it.

## The residual — `g(n_col)`, and why it belongs to row-LT

The curvature was *mostly* the measurement (occupancy + II=1 compute are exact).  The sole
remaining non-linearity is `g(n_col)` — the **per-row** pipeline / 2-buffer ping-pong depth.  At
**block (matrix) granularity** the model can only see it as `n_row·g(n_col)` and must carry `g`
as a measured 1-D curve.  At **row granularity** (row-LT) `g(n_col)` is simply the row's
pipeline depth, modeled once per row — the fit-free structural lift.  This is the concrete,
quantified case for row-LT: the residual is a single, physically-named, per-row quantity, not a
diffuse curvature, and not a `sqrt` fudge.
