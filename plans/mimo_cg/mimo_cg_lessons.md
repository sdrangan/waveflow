# Lessons learned: CG massive-MIMO paper simulations

Companion to [`plans/mimo_cg/mimo_cg_paper_sims.md`](mimo_cg_paper_sims.md). The doer
appends an entry whenever something surprising is learned: a mistake, a failed
approach, a tool quirk, or a rule worth reusing. Newest entries go at the bottom.

## From setup and planning (2026-09-27 to 2026-09-29)

- **Vitis 2024.1 on Ubuntu 24.04 works with Waveflow, with two adaptations.**
  - Vivado 2024.1 ships the XSI engine as `librdi_simulator_kernel.so`, but
    `waveflow/build/xsi/xsi_bfm.h` loads the 2025.1 name
    `libxv_simulator_kernel.so`. The `.venv/xsi_compat/` symlink, put on
    `LD_LIBRARY_PATH` by `source .venv/bin/activate`, bridges it.
  - `run.sh` finds Vivado through `XILINX_VIVADO`, which the same activation
    exports.

  Without activating the venv, every XSI run fails with "Failed to Load up XSI".
- **2024.1 does not reproduce every 2025.1 number.**
  - Co-simulation reports for AXI-Lite kernels count cycles differently:
    `examples/regmap` gives 49 on 2024.1 against 5 recorded on 2025.1.
  - XSI BFM cycle counts did reproduce: `mem_r_stream` gave 158 on both.
  - `test_amd_tools` exits 1 ("TOO OLD") by design; the 2025.1 floor is
    reported, not enforced.
- **The root `.gitignore` is broad.** It swallows `*.json`, `*.log`, `*.xml`,
  `*.rpt`, `*.bin`, any name containing `summary`, and any directory named `hls/`
  or `syn/`. Committed data goes in CSV, and the C++ goes in `cpp/`. Check with
  `git check-ignore -v <path>` before relying on a file being tracked.
- **`plans/cg.md`'s CG sketch is wrong.** It has two sign errors, a no-op
  `rnorm` update and a 1-D `X`, and its error stays near 1.5. The corrected
  version is in the plan's §9.
- **Pick SNR grids from the convention, not habit.** With per-user transmit SNR
  ρ = Es/σ² and 32–128 antennas, BER 1e-3 is reached between −11 and +11 dB, so
  a 0–30 dB grid misses it in most configurations.
- **Toolchain tests skip, rather than fail, when prerequisites are missing.**
  Only "N passed, 0 skipped" under `-rs` counts as evidence.
- **`SweepRunner` writes only `results/sweep.json`.** Per-point data must be
  written by the stage's own DAG step, and a dry run needs its own `--out`.
- **The build DAG skips up-to-date steps.** A determinism check must re-run with
  `--force`.
- **`FixedField` is capped at 64 bits** (int64 storage). Dot-product
  accumulators grow by log2(length) bits.

## Phase 0 (2026-09-30)

- **Don't pass `-q` to pytest in this repo.** `pyproject.toml` already sets
  `addopts = "-q ..."`, so another `-q` makes it `-qq`, which drops the
  "N passed" summary line. Use `-rs` without `-q` when the pass and skip counts
  are the evidence.
- **The bit-exact conformance suites hold on Vitis 2024.1.** 89 passed, 0 skipped
  in about 9 minutes (roughly 6 s per C-simulation), so the `-m vitis` regression
  in §11 is cheap enough to run at every milestone.
- **Vitis creates a `logs/` directory in its working directory.** Waveflow's
  runner defaults that directory to the TCL's folder (`waveflow/toolchain/toolchain.py`,
  `run_vitis_hls`), so in practice the two coincide. The example `.gitignore`
  covers it; any new directory that runs Vitis needs the same.
- **The readiness gate leaves `xelab.log` and `xelab.pb` in the repo root.** The
  `xelab --version` probe writes them to the current directory. Both are
  gitignored; delete them after the gate.
- **Log overlapping steps as a deviation.** Preparing one step while another's long
  run is in progress breaks "one step at a time" even when the commits stay in
  order. Record it in §15.

## Phase 1 (2026-09-30)

- **Ruff 0.16.9 here goes beyond the E/F defaults.** It flags `RUF046`
  (`int(round(x))`: `round` already returns an int) and `FURB161` (use
  `int.bit_count()`). Write new code that way from the start.
- **Black 26 warns "Python 3.12 cannot parse code formatted for Python 3.15".**
  The warning is harmless as long as `black --check` reports the files unchanged.
- **Near convergence, CG's rounding sets any "equals" tolerance.** Two correct CG
  implementations (gemm vs gemv, or 1 vs 32 columns in one gemm) agree to about
  1e-15 early on, but only to about their own convergence error (up to 1.6e-10
  here) at nit = K. Compare iterates tightly before convergence and loosely at
  it, or compare each against the exact solve.
- **Worker processes barely beat multithreaded BLAS here.** A serial timing that
  already used all 8 cores through BLAS predicted about 5 minutes with 8 pinned
  workers; the real run took 29 minutes. Time a single-threaded worker before
  estimating a parallel run.
- **`--through X` runs only X's ancestors.** A final step must consume every table
  its acceptance criterion expects, or those tables silently go unbuilt.
- **Render the figures and look before committing.** The PNG preview caught a
  title/legend collision, a curve hidden under another, and a legend label valid
  for only one panel. None of those show in tests or checksums.
- **Pick a statistical test's operating point from the theory table.** A point
  above the ZF crossing gives too few errors for a comparison to mean anything.

## M1 review (2026-09-30)

- **The DAG decides staleness from source-file timestamps.** A docstring-only edit
  to `mimo_cg.py` or `detectors.py` re-runs the 30-minute tables. Batch edits to
  model sources, and run `--status` before a build to see what will run.
- **Intervals counted over channel blocks are too wide to compare detectors.**
  They are valid, but at BER 1e-3 the 99.9% interval spans two orders of
  magnitude. Compare detectors on identical samples (paired), and use
  theory-crossing agreement as the accuracy evidence.
- **One scale for every detector is not neutral.** Exact MMSE's μ mis-scales CG
  iterates, because CG is nonlinear in its right-hand side, which leaves CG
  slightly pessimistic at 2–3 iterations. A "same post-processing for all"
  argument needs checking, not assuming.
- **After stopping a background run, check for orphaned workers and verify the
  outputs by hash.** `write_table` writes only at the end of a step, so a stopped
  run left the committed tables intact.
