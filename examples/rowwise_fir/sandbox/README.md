# Row-wise FIR — hand-written HLS load/compute/store sandbox

Phase 1 of [`plans/dataflow_composition.md`](../../../plans/dataflow_composition.md).
A **self-contained, hand-authored** Vitis HLS kernel (no Waveflow framework code)
that grounds the dataflow-composition arc: it builds the principled
**load–compute–store dataflow** structure for a per-row floating-point FIR and
validates its timing on real RTL, so the later Waveflow `dataflow` exec_model has
a ground-truth kernel to reproduce.

## What it computes

Real-valued **float** FIR per matrix row, *valid* edge handling:

```
Y[i, j] = sum_{t=0..T-1} h[t] * X[i, j + (T-1) - t],   j in [0, n_cols - T]
```

`T = 8` taps, `NCOL_MAX = 1024` row-buffer bound. The sliding window forces a
**resident, randomly-addressable row buffer** (you cannot FIR a pure stream), so
load-compute-store is a *correctness necessity*, not a nicety — the cleanest
motivation for the structure.

## The kernel (`fir_sandbox.cpp`)

The top `fir_accel` is a per-row `#pragma HLS DATAFLOW` region wiring three
concurrent modules — the hardware realization of three SimObjs over channels:

```
fir_load_row --xbuf (partitioned-BRAM ping-pong)--> fir_compute_row --yfifo (FIFO)--> fir_store_row
```

- **`fir_load_row`** burst-reads one row of `X` (sequential addresses ⇒ inferred
  AXI burst) plus the `T` taps into BRAM channels.
- **`fir_compute_row`** runs the FIR at **II=1** from BRAM only (taps + the T-tap
  window read in parallel via cyclic `ARRAY_PARTITION`); no external memory, so
  throughput is set by the load/store streams, not compute.
- **`fir_store_row`** burst-writes one output row.

`xbuf` is a partitioned-BRAM ping-pong (PIPO, depth `N_PINGPONG=2`) because
compute needs **windowed random access**; `yfifo` is a FIFO because compute→store
is **sequential** — the BRAM-channel vs FIFO-channel distinction. Putting the
`#pragma HLS DATAFLOW` in the row-loop body lets Vitis double-buffer the channels
across rows (load row r+1 overlaps compute row r overlaps store row r-1).

## What it proves

See `results/` for the committed artifacts and exact figures.

1. **csim bit-exact** — the C output equals the numpy golden bit-for-bit (the
   golden accumulates taps in the same left-to-right order as the kernel, so no
   tolerance is used). RTL cosim also passes bit-exact.
2. **Compute II = 1** — `results/csynth_*.json`: the `COMPUTE` loop (and the
   `LOAD`/`STORE` burst loops) pipeline at II=1.
3. **Bursts inferred** — `results/csynth_*.json` `inferred_bursts`: the row read
   (`LOAD`/`X`) and row write (`STORE`/`Y`) coalesce into AXI bursts.
4. **Calibrated cycle model** — `results/cosim_sweep.json`: RTL cosim over a
   `(n_row, n_col)` grid. The per-row `DATAFLOW` overlaps rows, so latency is
   **bilinear** in `(n_row, n_col)` — the plan's single-II `L0 + n_row·L_row +
   II·trips` form does not fit; the refined fit
   `L0 + L_row·n_row + L_col·n_col + II·trips` (R²≈0.99) and the measured
   steady-state **≈1.25 cycles/output** are what the later `block`-fidelity model
   consumes. See `results/fir_sandbox_results.md` for the honest accounting,
   including where this deviates from the plan's authored acceptance targets.

### Correction to the plan's hypothesis (a real finding)

The plan expected a **single shared `m_axi` bundle ⇒ II=2** (X-read + Y-write
contend) versus **split bundles ⇒ II=1**. Cosim shows the single-port and
split-bundle builds are **byte-for-byte identical in latency**: a single `m_axi`
bundle is **full-duplex** (independent AR/R and AW/W channels), so a *read+write*
pair never contends. The `#streams → II` floor (VMAC's II≈19 from two interleaved
*reads*) applies to **same-direction** streams sharing the single read channel —
not to a read+write pair. So the split-bundle knob buys nothing here; the
measured throughput floor (`II` in the fit) is set by the dataflow stage balance,
not bundle sharing. `results/cosim_sweep.json` records the single-vs-split
equality on the duplex-evidence points.

## Regenerate end-to-end

Use the project venv (`../../../pysilicon-venv/Scripts/python.exe`). Vitis HLS
2025.1 is driven via `vitis-run --mode hls --tcl run.tcl`.

```bash
# 1. Fixtures + golden (writes data/{X,h,Y_golden}.bin, meta.json, dims.tcl)
python gen_data.py

# 2. csim (bit-exact) + csynth x2 (single-port + split). Add cosim with the env:
vitis-run --mode hls --tcl run.tcl
WAVEFLOW_ROWWISE_FIR_COSIM=1 vitis-run --mode hls --tcl run.tcl   # + RTL cosim

# 3. Parse csynth -> results/csynth_{singleport,split}.json
PYTHONPATH=../../.. python extract_results.py

# 4. Cosim calibration sweep -> results/cosim_sweep.json  (slow: many cosims)
PYTHONPATH=../../.. python cosim_sweep.py
```

`run.tcl` mirrors `examples/block_scale/run.tcl` (`WAVEFLOW_SUCCESS:` /
`WAVEFLOW_ERROR:` sentinels, env-gated cosim). The build dirs
(`waveflow_rowwise_fir_proj/`, `..._split_proj/`, `sweep/`) and logs are
gitignored; everything under `results/` is committed.

## Files

| File | Role |
|---|---|
| `fir_sandbox.hpp` / `.cpp` | params + the 3-module DATAFLOW kernel |
| `fir_tb.cpp` | testbench: load `data/`, run, compare vs golden (bit-exact) |
| `run.tcl` | csim + csynth (single-port & split) + optional cosim |
| `gen_data.py` | numpy fixtures + bit-exact golden + `dims.tcl` |
| `extract_results.py` | csynth reports → `results/csynth_*.json` |
| `cosim_sweep.py` | cosim grid → fitted cycle model → `results/cosim_sweep.json` |
| `data/` | committed fixture (`n_rows=4, n_cols=64`) |
| `results/` | committed artifacts (the deliverables) |
