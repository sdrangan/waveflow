# Lessons learned: CG massive-MIMO paper simulations

Companion to [`plans/mimo_cg_paper_sims.md`](mimo_cg_paper_sims.md). The doer
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
- **Vitis creates a `logs/` directory beside the TCL it runs.** The example
  `.gitignore` covers it; any new directory that runs Vitis needs the same.
