# FIR sandbox — Phase 1 results

Hand-written HLS load–compute–store FIR (`plans/dataflow_composition.md` Phase 1).
Float, *valid* edge, `T=8`, `NCOL_MAX=1024`, part `xc7z020clg484-1`, 10 ns clock,
Vitis HLS 2025.1. All figures below are backed by the committed artifacts
`csynth_singleport.json`, `csynth_split.json`, `cosim_sweep.json`.

## Headline results (confirmed)

| Claim | Result | Evidence |
|---|---|---|
| **csim bit-exact** | C output == numpy golden, exact (no tolerance); RTL cosim also bit-exact | `fir_tb.cpp` PASS, cosim `Pass` |
| **Compute II = 1** | `COMPUTE` inner loop achieved **II=1** (pipelined) | `csynth_*.json` `loop_ii.COMPUTE` |
| **All loops II = 1** | `LOAD`, `COMPUTE`, `STORE`, `LOAD_TAPS` all II=1 | `csynth_*.json` `loop_ii` |
| **Bursts inferred** | row read (`LOAD`/X) and row write (`STORE`/Y) coalesce to AXI bursts; taps (`LOAD_TAPS`/h) too | `csynth_*.json` `inferred_bursts` |

The dataflow datapath is exactly as designed: the compute stage runs at II=1 from
BRAM only (taps + the T-tap window read in parallel via cyclic partitioning), and
the load/store stages coalesce their sequential row accesses into AXI bursts.

## Correction to the plan's II hypothesis (a real finding)

The plan predicted **single shared `m_axi` bundle ⇒ II=2** (X-read + Y-write
contend) vs **split bundles ⇒ II=1**. **This does not hold.** Single-port and
split-bundle builds cosim to **byte-for-byte identical** latency:

| size | single-port cycles | split cycles | identical |
|---|---|---|---|
| 1×64 | 272 | 272 | ✓ |
| 4×256 | 2383 | 2383 | ✓ |

A single `m_axi` bundle is **full-duplex** — independent AR/R and AW/W channels —
so a *read + write* pair never contends, and splitting X/Y onto separate bundles
buys nothing. The `#streams → II` floor (VMAC's II≈19 from two interleaved
*reads*) applies to **same-direction** streams sharing the one read channel, not
to a read+write pair. cosim faithfully models same-direction contention (that is
VMAC's result), and faithfully shows none here.

## Calibrated cycle model

RTL cosim over a `(n_row, n_col)` grid (`n_col ∈ {16,32,64,128,256,512}`,
`n_row ∈ {1,4}`), held-out point `(4,1024)`:

| n_row | n_col | trips | RTL cycles |
|---|---|---|---|
| 1 | 16 | 9 | 128 |
| 1 | 64 | 57 | 272 |
| 1 | 512 | 505 | 1367 |
| 4 | 64 | 228 | 655 |
| 4 | 256 | 996 | 2383 |
| 4 | 512 | 2020 | 3692 |
| 4 | 1024 (held out) | 4068 | 6252 |

(full grid in `cosim_sweep.json`)

**The plan's model `L0 + n_row·L_row + II·trips` (single global II≈2) does not fit**
(R²=0.979, held-out error 21%). The reason is structural: per-row `#pragma HLS
DATAFLOW` overlaps rows, so the **per-row interval scales with `n_col`** — latency
is *bilinear* in `(n_row, n_col)`, not single-II affine. The refined fit adds an
`L_col·n_col` pipeline-fill term:

```
latency_cycles ≈ L0 + L_row·n_row + L_col·n_col + II·trips
              = 68.6 + 60.2·n_row + 1.03·n_col + 1.49·trips      (R² = 0.987)
```

The directly-measured **steady-state throughput is ≈ 1.25 cycles/output** (the
slope between the two largest points, overhead amortized) — **identical for
single-port and split**. There is no II=2 floor; the throughput floor for this
full-duplex read+write kernel is ≈1.25 cyc/output, set by burst/dataflow stage
balance, not bundle sharing.

## Deviations from the plan's Phase-1 acceptance criteria (with reasons)

The plan's authored acceptance list assumed an II=2/II=1 contrast that the
hardware does not exhibit. Honest status of each item:

- ✅ **csim bit-exact** — met.
- ✅ **Compute II=1** — met.
- ⚠️ **Single-port floor II=2** — **not met; refuted.** Single bundle is
  full-duplex; effective throughput ≈1.25 cyc/output, not 2. (Evidence committed.)
- ⚠️ **Split-bundle II=1 (distinct from single)** — **not met; refuted.** Split
  is identical to single-port (no contention to relieve). (Evidence committed.)
- ⚠️ **Calibrated model R²≳0.999, held-out ≲2%, fitted II≈2** — **not met as
  specified.** The model *form* is mis-specified for per-row dataflow (bilinear,
  not single-II); best affine fit R²=0.987, and the `(4,1024)` held-out point is a
  2× `n_col` extrapolation into the still-amortizing regime → 19% error. The
  honest, reproducible characterization is the bilinear fit above plus the
  measured ≈1.25 cyc/output steady-state throughput.

These are recorded so the downstream Waveflow `dataflow`/`block`-fidelity work
calibrates against reality: **compute II=1, full-duplex single-bundle, ≈1.25
cyc/output steady-state.** The `#streams→II` floor remains valid for
same-direction streams (VMAC), which is the correct framing to carry forward.
