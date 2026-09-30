# Plan: simulations for the CG massive-MIMO DSE paper

**Status:** In progress (Phase 0 started 2026-09-30)
**Complexity:** L3 Complex. Many components (link simulator, fixed-point division, bit-exact CG, three hardware blocks, performance models, DSE), several open design choices, and new tests throughout; everything is undoable with git. Scores: scope 3 · clarity 3 · novelty 3 · dependencies 3 · verification 3 · risk 2
**Created:** 2026-09-29 · **Updated:** 2026-09-30
**Plan file:** `plans/mimo_cg_paper_sims.md` · **Lessons learned:** `plans/mimo_cg_lessons.md` (created in step 0.1)

> **For the agent executing this plan:** read *Rules for the doer* first. The
> plan is self-contained. Paths are repo-relative, commands are exact, and
> secrets are named by environment variable only (this plan needs none).

## Rules for the doer

1. Before changing anything, re-run the readiness commands in §8. Stop if
   anything required is ❌ blocked. Run every Waveflow, pytest or Vitis command
   inside the activated venv: `source .venv/bin/activate` (it also sets
   `XILINX_VIVADO` and `LD_LIBRARY_PATH`, which the XSI flow needs).
2. Do one step at a time (§10). A step is done only when its exit condition is
   verified with the stated method. Show the command and its result as evidence.
   Before changing code, read it and its callers. Check the authoritative docs
   for any version-sensitive API (versions in §6). Before building a tool,
   script or integration the plan didn't foresee, search for an existing one
   and propose it first.
3. After each step, tick it in §10, add a line to the §15 progress log, and
   append anything surprising to `plans/mimo_cg_lessons.md`.
4. When something fails, find the root cause from the error, logs, tests and
   actual state. No random fixes. Deviation policy (L3 default, confirm in
   execute mode): investigate up to 2 tested hypotheses, then stop and report.
   **Never change a test's random seed, tolerance or confidence level to make it
   pass**; a statistical failure is investigated like any other.
5. Pause for the user after each milestone (M0–M6), and on surprises. Steps
   marked 👁 also wait for the user's review.
6. Never perform an action listed in §12 as irreversible or costly without its
   backup in place and fresh approval from the user at that moment.
7. Never merge, push, deploy or publish unless the user asks.
8. State assumptions and anything you couldn't verify. Never fill a gap with a
   guess.
9. **Commits are the user's.**
   - Author and committer are the repo's effective git identity,
     `ali-rasteh <ali.rasteh1@gmail.com>` (confirmed by the user 2026-09-29).
     Never pass `--author`, never set `GIT_AUTHOR_*` / `GIT_COMMITTER_*`, never
     change git config.
   - End every commit message with a `Co-Authored-By:` trailer naming the
     Claude model that made the commit, e.g.
     `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (user decision
     2026-09-29).
   - Never stage `.gitignore` (the user's own uncommitted edit) or
     `docs/repo_docs/`. Stage files by explicit path, never with `git add -A`
     or `git add .`.
10. Every number that could appear in the paper comes from Vitis/Vivado
    **2024.1** at `/tools/Xilinx`. Record the tool version in every results file
    the plan produces. Never overwrite the repo's committed 2025.1-era gate
    numbers or calibration corpora; 2024.1 measurements go into new files.
11. A toolchain test only counts when it **ran**: run `pytest -m vitis` and
    `pytest -m xsi` with `-rs`, and require "N passed, 0 skipped" for the tests
    an acceptance criterion names.
12. Finish with §16: check every acceptance criterion, run the regression check,
    and write the completion report.

## 1. Goal

Produce the simulation evidence for a paper arguing that **one Waveflow Python
source** yields both (a) an **exact** accuracy design-space exploration (DSE),
because the functional model is bit-exact to the hardware, and (b) a fast,
**calibrated, approximate** performance DSE, with full Vitis runs used only for
calibration and sparse validation. The wireless workload is **MMSE detection
for massive-MIMO uplink, solved by conjugate gradient (CG)** on the regularized
Gram matrix `A = HᴴH + σ²I`, targeting the RFSoC 4x2 part (`xczu48dr`).

The headline result, from `plans/paper_cg_dse_vision.md`: explore N design
points with K ≪ N full Vitis runs, show the DSE conclusions match brute-force
Vitis on a held-out subset, and report a concrete, non-obvious design finding
(for example "12-bit and 4 iterations reach the target BER at half the DSPs of
a naive 16-bit, 8-iteration design"). Writing the manuscript is out of scope;
this plan delivers the data, figures and code that back it.

## 2. Requirements and constraints

| Type | Item |
|---|---|
| Must | Link-level uplink simulator: i.i.d. Rayleigh flat fading, perfect CSI, uncoded; M ∈ {32, 64, 128} base-station antennas; K ∈ {4, 8, 16} users; QPSK, 16-QAM, 64-QAM (Gray); target BER 1e-3. |
| Must | SNR convention fixed and documented: per-user transmit SNR ρ = Es/σ² with unit-energy constellations (Es = 1) and H entries CN(0, 1), so σ² = 1/ρ and `A = HᴴH + σ²I`. For QPSK the per-bit SNR is ρ/2. |
| Must | SNR grids that reach the target under that convention. The analytical ZF BER = 1e-3 crossing, computed at planning (§9), ranges from −11.1 dB (M=128, K=4, QPSK) to +10.9 dB (M=32, K=16, 64-QAM). Floating-point runs sweep ρ from −20 to +20 dB in 1 dB steps. Phase 3 uses a per-configuration window of ±6 dB around that configuration's analytical ZF crossing, in 1 dB steps. |
| Must | Detectors: ZF, exact MMSE (`numpy.linalg.solve`), and CG-MMSE with the iteration count `nit` as a parameter. CG runs as plain CG (one right-hand side per received vector) and as **multi-RHS CG** (CG applied to every column of a matrix right-hand side, `B = HᴴY` or `B = I`). The hardware targets multi-RHS CG, because its `A @ P` is a matrix-matrix product, which is what justifies a systolic array. O'Leary's true block-CG (shared search space) is out of scope unless chosen at gate 2.1. |
| Must | The floating-point reference is validated against closed-form theory (§4 AC1.1, AC1.2) before anything is built on it. |
| Must | Bit-exact fixed-point CG detector on `FixedField`/`ComplexField`, proven bit-for-bit equal to Vitis 2024.1 C-simulation of the same algorithm, including a defined behaviour for a zero divisor. |
| Must | Accuracy DSE runs with **no Vitis in the loop** (Phases 1–3), through `ParamGrid`/`SweepRunner`, proven by a test. |
| Must | Hardware blocks per the vision's fixed architecture: matrix-multiply block, vector unit, shared memory plus queue, CG control. Target `xczu48dr-ffvg1517-2-e` at 250 MHz (4 ns), the repo's RF constants (`RFSOC4X2_PART` in `waveflow/build/composite_gen.py`). |
| Must | Per-block resource and cycle models, validated on **held-out** design points that are fixed before fitting, with a stated calibration method. |
| Must | Every figure and data table is regenerated by one documented command, with deterministic seeds, and a forced re-run reproduces it byte for byte. |
| Must not | Commit as anyone but the user; merge or push; stage `.gitignore` or `docs/repo_docs/`. |
| Must not | Change files outside this repo, or install anything outside `.venv/`. The only exception is the state the AMD tools keep under the home directory (e.g. `~/.Xilinx`). Adding a dependency to `pyproject.toml` needs the user's approval first. |
| Must not | Overwrite the repo's 2025.1-recorded gate numbers or calibration corpora. |
| Must not | Reformat existing framework files (several already fail `black` and `ruff`; §6). |
| Must not | Reuse the CG code in `plans/cg.md` as written: it has sign errors and does not converge (§9). |
| Nice to have | Correlated channels (e.g. exponential or Kronecker correlation) as a second scenario. |
| Nice to have | Uncertainty- or decision-aware selection of which designs to synthesize (vision §"When to spend a synthesis"). |
| Nice to have | Jacobi-preconditioned CG as an extra accuracy axis. |
| Nice to have | Later, a system-context scenario feeding the detector from the RF track (`examples/rf_shot_loopback`). Not planned here. |
| Constraint | No deadline. |
| Constraint | Brute-force Vitis baseline budget: at most about 48 hours of csynth on this machine (8 cores, 31 GB RAM). `SweepRunner` runs serially. |
| Constraint | Vitis/Vivado 2024.1 for every number. Python 3.12 in `.venv/`. Follow the repo's conventions (§7) and `CLAUDE.md`. |

## 3. Deliverables

| Deliverable | Format | Location | For whom |
|---|---|---|---|
| This plan, kept current | Markdown | `plans/mimo_cg_paper_sims.md` | the user, the doer |
| Lessons learned | Markdown | `plans/mimo_cg_lessons.md` | future sessions |
| Example-level ignore rules for build outputs | `.gitignore` | `examples/mimo_cg/.gitignore` | keeps build outputs out of git without touching the root `.gitignore` |
| xczu48dr probe (sources committed, outputs ignored) | C++ + TCL | `examples/mimo_cg/tools/xczu48dr_probe/` | reproducible toolchain check |
| Link simulator, detectors, bit-exact CG golden, build DAG, sweeps, figure scripts | Python | `examples/mimo_cg/` | co-authors, reviewers |
| C++ reference of the CG detector (conformance) and, later, the hardware blocks | C++ (HLS) | `examples/mimo_cg/cpp/` (**not** `hls/`, which the root `.gitignore` swallows) | conformance tests, Vitis |
| Fixed-point division or reciprocal | Python (+ C++ side if needed) | `waveflow/hw/fixpoint.py`, `waveflow/utils/fixputils.py` | framework users |
| Division conformance, added to the existing fixed-point harness | Python + generated C++ | `examples/schemas/fixedpoint/` (`render_binop` already takes the operator), `tests/hw/test_fixpoint.py`, `tests/hw/test_fixpoint_vitis.py` | CI |
| New tests | pytest | `tests/examples/test_mimo_cg_*.py` | CI, reviewers |
| Paper data (committed) | CSV (never JSON, never a name containing `summary`, both gitignored) | `examples/mimo_cg/paper_data/` | co-authors |
| Paper figures (committed) | SVG | `docs/examples/mimo_cg/images/` | co-authors |
| Example docs pages | Markdown | `docs/examples/mimo_cg/` | readers of the docs site |
| xczu48dr calibration platform | Waveflow calib library | `examples/mimo_cg/calib/platforms/xczu48dr_250mhz/` | performance models |
| Corrected CG sketch and refreshed status table | Markdown edits | `plans/cg.md`, `plans/paper_cg_dse_vision.md` | the team |

## 4. Acceptance criteria

The task is done when every box is ticked with the evidence named. The Phase
4–6 criteria are provisional: gates 4.0, 5.0 and 6.0 may tighten them, and any
loosening needs the user's approval and a §14 entry. Statistical checks use 99.9%
Wilson intervals with seeds fixed in the test file (Rules 4).

- [ ] **AC0.1**: §15 records `vitis-run --version` and `vivado -version` both at 2024.1, and the committed xczu48dr probe prints `PROBE_CSYNTH_OK`. `pytest -m vitis -rs tests/examples/test_fixedpoint_conformance.py tests/examples/test_complex_conformance.py tests/hw/test_fixpoint_vitis.py -q` then either reports N passed and 0 skipped, or every failure is root-caused, recorded in §14 as a 2024.1 difference, and accepted by the user before Phase 2. Verify: steps 0.2 and 0.3.
- [ ] **AC0.2**: `plans/cg.md` opens with a "Superseded" banner pointing to `examples/mimo_cg/`, and its algorithm is corrected. `plans/paper_cg_dse_vision.md`'s build-vs-have table reflects the state as of step 0.4. The corrected algorithm passes the check script in §9: relative error ≤ 1e-10 at `nit = 8`, against 2.0e-16 measured at planning. Verify: run the §9 script; user reviews the diff.
- [ ] **AC1.1**: `pytest tests/examples/test_mimo_cg_link.py -q` passes. It checks the constellations: mean energy within 1e-12 of 1, Gray labelling (neighbours differ in exactly one bit), and the mapper/demapper round trip. It also checks AWGN-only (H = I) BER against the exact Gray square-QAM BER of Cho and Yoon (2002), with 99.9% Wilson intervals, at these 9 points (Es/N0 = ρ):

  | Modulation | ρ (dB) | Expected BER |
  |---|---|---|
  | QPSK | 0 / 4 / 8 | 1.587e-1 / 5.650e-2 / 6.004e-3 |
  | 16-QAM | 6 / 10 / 14 | 1.414e-1 / 5.899e-2 / 9.376e-3 |
  | 64-QAM | 12 / 16 / 20 | 1.146e-1 / 4.917e-2 / 8.486e-3 |

- [ ] **AC1.2**: `pytest tests/examples/test_mimo_cg_detectors.py -q` passes, covering:
  - (a) ZF with QPSK in i.i.d. Rayleigh at M = 32, K = 16 lies inside the 99.9% interval of the closed-form MRC BER with L = M−K+1 = 17 branches and per-bit SNR ρ/2: 1.005e-1 at −10 dB and 1.328e-2 at −5 dB.
  - (b) Plain CG and multi-RHS CG match `numpy.linalg.solve` to relative error ≤ 1e-8 at `nit = K`, for (M, K) ∈ {32, 64, 128} × {4, 8, 16} and seeds 0–9.
  - (c) The A-norm error is non-increasing: e₍ⱼ₊₁₎ ≤ e₍ⱼ₎·(1 + 1e-10) + 1e-14·‖x‖_A.
  - (d) The recurrence and explicit-residual forms agree to relative 1e-8 at `nit = K`.
  - (e) Column j of multi-RHS CG equals plain CG on column j to relative 1e-12.
- [ ] **AC1.3**: `python -m examples.mimo_cg.mimo_cg_build --through float_figures --force`, run twice, gives identical `sha256sum` for `examples/mimo_cg/paper_data/float_ber.csv`, `float_ranges.csv` and every SVG under `docs/examples/mimo_cg/images/`. Each Monte Carlo point is seeded from a `numpy.random.SeedSequence` keyed by the point's parameters, not by run order or worker count. SVGs are made deterministic with `matplotlib.rcParams['svg.hashsalt']` and `savefig(..., metadata={'Date': None})`.
- [ ] **AC2.1**: The Phase 2 decision record is in §14 and approved by the user. It covers the division method, the zero-divisor guard, the CG variant, A normalization, per-variable formats, and the "wide" formats with ≥ 24 fraction bits.
- [ ] **AC2.2**: The fixed-point division/reciprocal op is bit-exact to Vitis 2024.1 on at least 1000 random operand pairs across at least 3 formats. The pairs include small divisors, saturation, negative values and a zero divisor, whose behaviour is the one defined by the guard. Verify: `pytest tests/hw/test_fixpoint.py -q` and `pytest -m vitis -rs tests/hw/test_fixpoint_vitis.py -q` (N passed, 0 skipped).
- [ ] **AC2.3**: The Python bit-exact CG golden and the C++ reference in `examples/mimo_cg/cpp/` give bit-for-bit equal outputs under Vitis 2024.1 C-simulation for `xczu48dr-ffvg1517-2-e`. Coverage: at least 50 random cases for each of at least 3 format sets, plus at least 1 zero-residual case in which a column's residual quantizes to exactly zero. Verify: `pytest -m vitis -rs tests/examples/test_mimo_cg_conformance.py -q` (N passed, 0 skipped).
- [ ] **AC2.4**: At M = 64, K = 8 over ≥ 20 seeds, the wide formats give X within relative error 1e-4 of floating-point CG at every `nit` ≤ K. The bit-exact detector's BER lies inside the 99.9% interval of the floating-point CG BER at 3 SNR points of the default configuration. Verify: `pytest tests/examples/test_mimo_cg_fixed.py -q`.
- [ ] **AC3.1**: The accuracy sweep behaves as specified:
  - `python -m examples.mimo_cg.mimo_cg_accuracy_sweep --dry-run --out results/dry_run.json` prints the number of kept points, after the `nit ≤ K` filter (`Stage.when`).
  - The full run completes; after any interruption, `--resume` continues it.
  - The merged `examples/mimo_cg/paper_data/accuracy_grid.csv` has exactly (kept points × 13 SNR values) data rows.
  - `pytest tests/examples/test_mimo_cg_sweep_no_vitis.py -q` passes. It runs a small sweep with `waveflow.toolchain.toolchain.run_vitis_hls`, `subprocess.run` and `subprocess.Popen` patched to raise.
- [ ] **AC3.2**: `examples/mimo_cg/paper_data/accuracy_frontier.csv` lists, for every (M, K, modulation), the smallest (width, `nit`) whose SNR loss at BER 1e-3, against floating-point exact MMSE, is ≤ 0.5 dB. The loss is interpolated in log-BER, and a design that never reaches 1e-3 in its window is marked `floor`, not dropped. The accuracy figures come from one command, and a `--force` re-run is byte-identical.
- [ ] **AC3.3**: A written candidate finding is in §14, reviewed by the user.
- [ ] **AC4**: Each hardware block (vector unit, matrix multiply, CG control with shared memory and queue) passes three gates:
  - bit-exact to the Phase 2 golden in Python simulation;
  - csynth on `xczu48dr-ffvg1517-2-e` with estimated clock ≤ 4 ns;
  - bit-exact at RTL (co-simulation or XSI) on at least one configuration.

  The integrated detector is also bit-exact at RTL on at least M = 32, K = 4. Verify: the tests named at gate 4.0, with N passed and 0 skipped.
- [ ] **AC5**: The held-out split is written to `examples/mimo_cg/paper_data/holdout_split.csv` before any fitting. It contains ≥ 10 held-out configurations per block and ≥ 5 held-out full designs, none used in fitting. On them:
  - DSP and BRAM predictions are exact on ≥ 90% of the block configurations;
  - LUT/FF mean absolute percentage error is ≤ 10% on the full designs;
  - cycle mean absolute percentage error is ≤ 5% on the full designs.

  Verify: `examples/mimo_cg/paper_data/model_validation.csv` and its generating command.
- [ ] **AC6**: The full DSE computes the predicted Pareto frontier (accuracy vs DSP/LUT/BRAM vs latency) over the whole cross-product in Python. A brute-force Vitis subset is run within the 48-hour budget. Provisional fidelity measure: for ≥ 90% of the accuracy targets in that subset, the model's minimum-DSP design meeting the target either is the brute-force minimum-DSP design or is within 10% of its DSP count. A cost table (Vitis hours vs Waveflow minutes plus K calibration runs) and the design finding are in `paper_data/` and §16. Verify: the commands recorded in step 6.0.
- [ ] **AC-R**: No regressions.
  - `pytest -m "not vitis and not xsi" -p no:cacheprovider` fails only on the 7 pre-existing failures listed in §6.
  - Every **new** Python file passes `ruff check` and `black --check`.
  - Every **changed existing** file has no more `ruff check --output-format concise` findings than on `main`, and is not reformatted.

## 5. Context and sources of truth

| Source | What it is | Access | Freshness / version |
|---|---|---|---|
| `plans/paper_cg_dse_vision.md` | The paper's thesis, fixed architecture, parameters, methodology, reviewer risks | read the file | 2026-07-04; its build-vs-have table is stale (refreshed in step 0.4) |
| `plans/cg.md` | CG matrix-inverse sketch and two-block architecture | read the file | **buggy code**; use §9 instead |
| `plans/resource_model.md`, `plans/sweep_runner.md`, `plans/harmonize_calib.md` | How the resource model, sweeps and calibration were built | read the files | resource model phases A–E complete; sweep P1–P5 done; harmonize done (header stale) |
| `plans/vitis_l1_hwmodule.md` | HwModule wrappers for Vitis L1 FFT/GEMV, owner Amir Reza Kiani | read the file | S0 done, S1+ open (2026-09-20) |
| `plans/integrate_blas.md` | BLAS integration plan | read the file | header says not started; GEMV bit-exact part since done in `waveflow/vitis_l1` |
| `docs/guide/schema/python/fixpoint.md`, `complex.md` | FixedField / ComplexField user docs | read | current |
| `docs/guide/build/sweep.md`, `docs/guide/calib/`, `docs/guide/timing_model/`, `docs/guide/resource_model/` | Sweep, calibration, timing and resource model guides | read | current |
| `docs/repo_docs/` | Generated repo overview (architecture, data flow, module map) | read (untracked) | 2026-09-27; its toolchain page describes a Windows laptop, the architecture pages apply here |
| `examples/vmac/` | Complex fixed-point vector engine: `scalar_mult`, `inner_prod`, `sum` over an AXI-MM command ring, with cycle calibration | read | last code change 2026-07-27; docs say it is "due a rebuild on the interleaver's foundation" |
| `examples/fir_block/` | The methodology template: sweep → `InspectSynthStep` → resource model with held-out validation | read | DSP/BRAM exact on 24/24 points on xc7z020 |
| `examples/schemas/fixedpoint/` (`kernels.py::render_binop`, `fixedpoint_build.py`) with `tests/hw/test_fixpoint_vitis.py`; `examples/schemas/complex/` with `tests/examples/test_complex_conformance.py` | The bit-exact conformance harness that division joins | read | proven on 2025.1 elsewhere; re-checked on 2024.1 in step 0.3 |
| AMD UG1399, Vitis HLS 2024.1 user guide | `ap_fixed` semantics (division, quantization, overflow), pragmas, DSP inference | https://docs.amd.com (web access approved 2026-09-29) | 2024.1 edition |
| K. Cho and D. Yoon, "On the general BER expression of one- and two-dimensional amplitude modulations," *IEEE Trans. Commun.* 50(7):1074–1080, 2002 | Exact Gray square-QAM BER used in AC1.1 | web or library | standard result |
| MRC BER with L i.i.d. Rayleigh branches (e.g. Proakis, *Digital Communications*, diversity chapter); ZF post-processing SNR ~ ρ·Gamma(M−K+1, 1) | The AC1.2(a) closed form | textbook or web; cite in the test docstring | standard result |
| `docs/repo_docs/toolchain-readiness.md` | Why 2024.1 works here and what differs from 2025.1 | read | 2026-09-27 |

## 6. Environment

- **OS / shell:** Ubuntu 24.04.4 LTS, bash. 8 cores, 31 GB RAM, about 169 GB free on `/home`.
- **Languages and runtimes (versions):** Python 3.12.3 in `.venv/` at the repo root (gitignored). `source .venv/bin/activate` also exports `XILINX_VIVADO=/tools/Xilinx/Vivado/2024.1` and puts `.venv/xsi_compat/` on `LD_LIBRARY_PATH`. That directory holds a symlink `libxv_simulator_kernel.so` → Vivado 2024.1's `librdi_simulator_kernel.so`, because the XSI harness (`waveflow/build/xsi/xsi_bfm.h`) loads the 2025.1 name. System g++ 13.3.0 for XSI.
- **Frameworks and key dependencies (versions):** numpy 2.5.3, scipy 1.18.1, pandas 3.0.6, simpy 4.1.2, matplotlib 3.11.2, pydantic 2.13.5, pytest 9.1.1, ruff 0.16.9, black 26.5.1, mypy 2.3.1.
- **AMD tools:**
  - Vitis 2024.1: `/tools/Xilinx/Vitis/2024.1/bin/vitis-run`, found automatically by `waveflow.toolchain`, so `WAVEFLOW_VITIS_PATH` stays unset.
  - Vivado 2024.1: `/tools/Xilinx/Vivado/2024.1`.
  - Device data for `xczu48dr` is installed, and csynth for `xczu48dr-ffvg1517-2-e` at 4 ns was proven on 2026-09-29 (§8), with no licence message.
  - A licence file `~/.Xilinx/Xilinx.lic` exists. It was not opened. Vivado *implementation* on xczu48dr is untested.
- **Build / run / test commands:**
  - Fast suite: `pytest -m "not vitis and not xsi" -p no:cacheprovider`, about 90 s. The baseline is 3360 passed, 37 skipped, 7 failed; all 7 failures predate this plan:
    - `tests/build/test_rtl_module.py::test_shipped_memory_is_the_witness_plus_the_published_latency` expects CRLF line endings that a Linux checkout doesn't have.
    - `tests/hw/test_dataschema_poly.py::test_poly_notebook_flow_generates_headers_vectors_and_expected_outputs` reads `examples/stream_inband/poly.hpp`, which was never committed.
    - Five tests in `tests/poly/test_timing_analysis.py` (`test_tx_id`, `test_nsamp`, `test_x_first_value`, `test_x_last_value`, `test_y_values`) decode a VCD fixture recorded before the `PolyCmdHdr` schema changed on 2026-05-18.
  - Toolchain tests: `pytest -m vitis -rs …` and `pytest -m xsi -rs …`. XSI needs a prior csynth. Toolchain tests **skip**, rather than fail, when prerequisites are missing (Rules 11).
  - Example builds: `python -m examples.<name>.<name>_build --through <step> [--force]`. `--list-steps` lists the steps. `--force` re-runs steps the DAG would otherwise skip as up to date.
  - Lint: `ruff check <paths>`, `black --check <paths>`. On `main` today, `black` would reformat `waveflow/hw/fixpoint.py`, `waveflow/utils/fixputils.py` and `waveflow/calib/device_rules.py`, and `ruff` reports existing findings in them; do not reformat them (AC-R).
- **Sweep behaviour (`waveflow/build/sweep.py`):**
  - `SweepRunner` writes one summary, `results/sweep.json` by default; it writes no CSV and no log, so per-point data must be written by the stage's own DAG step.
  - `ParamGrid` is a plain Cartesian product; `Stage.when(point)` skips a stage for a point, which is how `nit ≤ K` is enforced.
  - `sweep_cli` offers `--dry-run`, which runs `dry_run_stages` on every point and writes the summary, `--resume` and `--out`. Always give a dry run its own `--out` so it doesn't overwrite the real run's resume record.
- **Project structure (the parts that matter):**
  - `waveflow/hw/`: DataSchema types, `FixedField`, `ComplexField`, HwModule, memory and queues.
  - `waveflow/build/`: build DAG, code generation, sweeps, resource steps.
  - `waveflow/calib/`: timing and resource models, device rules, calibration platforms.
  - `waveflow/vitis_l1/`: bit-exact FFT and GEMV models.
  - `examples/`: one directory per design.
  - `tests/`: mirrors the source tree.
  - `plans/`: plans.
  - `docs/`: the docs site.
- **Limitations:**
  - **2024.1 differs from 2025.1**, which the repo's recorded numbers assume:
    - `test_amd_tools` reports "TOO OLD" and exits 1 (expected; not enforced).
    - For AXI-Lite kernels the co-simulation report counts cycles differently: `examples/regmap` reports 49 on 2024.1 against 5 recorded on 2025.1.
    - XSI cycle counts reproduced exactly: the `mem_r_stream` gate gave 158 on both.
  - `.gitignore` ignores `*.json`, `*.log`, `*.xml`, `*.rpt`, `*.bin`, `*summary*`, and any `hls/` or `syn/` directory. Check with `git check-ignore -v <path>` before relying on a file being committed.
  - `FixedField` is capped at 64 bits. It supports only `AP_TRN`/`AP_RND` quantization and `AP_WRAP`/`AP_SAT` overflow, and has **no division, reciprocal or square root**.
  - `waveflow.vitis_l1.gemv` is real-valued only.

## 7. Existing system and reuse

- **Current implementation:** nothing CG- or wireless-specific exists. A search of `waveflow/`, `examples/` and `tests/` finds no QAM, OFDM, AWGN, BER, EVM, SNR-sweep or MIMO code. The RF track (`examples/rf_*`, `waveflow/hw/rfdc.py`) models converters and sample buffers, not baseband processing.
- **Reuse:**
  - Fixed-point: `from_real`, `to_real`, `mult`, `add`, `sub`, `shift`, `fixed_sum`, `quantize` in `waveflow/hw/fixpoint.py` (functions from line 141); `FixedField` → `ap_fixed` codegen in `waveflow/build/hwgen.py`.
  - Complex: `cadd`, `csub`, `cmult`, `conj`, `csum`, `cquantize` in `waveflow/hw/complexfield.py` (from line 328); C++ side in `waveflow/build/complex_utils.hpp` and `wf_cint.h`.
  - Conformance harness: `examples/schemas/fixedpoint/kernels.py::render_binop(op, …)` is already driven for `*` and `+` in `examples/schemas/fixedpoint/fixedpoint_build.py`, and division joins it there. `tests/hw/test_fixpoint_vitis.py` runs that harness. The complex harness is `examples/schemas/complex/` with `tests/examples/test_complex_conformance.py`.
  - Build DAG: `BuildDag`/`BuildStep` in `waveflow/build/build.py`, `run_dag_cli` in `waveflow/build/cli.py`; pattern in `examples/regmap/simp_fun_build.py`.
  - Sweeps: `ParamGrid`, `Stage` (with `when`), `SweepRunner`, `sweep_cli` in `waveflow/build/sweep.py`; pattern in `examples/fir_block/fir_block_sweep.py`.
  - Resource model: `InspectSynthStep` (`waveflow/build/resource_steps.py`); `waveflow/calib/device_rules.py`, which already has DSP48E2 geometry for UltraScale+; `VitisResourceModel` (`waveflow/calib/vitis_model.py`); `compose` (`waveflow/calib/resource_model.py`); confidence levels (`waveflow/calib/confidence.py`).
  - Cycle model: `TimingModel` (`waveflow/calib/timing_model.py`), `LinCalibModel` (`waveflow/calib/calib.py`), `CollectTimingStep`/`FitTimingStep` (`waveflow/build/calib_steps.py`); `waveflow_calib list|new|show|publish`.
  - Vector unit candidate: `examples/vmac/` (`OpCode.scalar_mult`, `inner_prod`, `sum` in `vmac_datatypes.py`; cycle fit in `vmac_calibrate.py`). It lacks per-column α and an AXPY op.
  - Shared memory and queue: `AXIMMQueue` (`waveflow/hw/aximm_queue.py`), `MemoryMod` (`waveflow/hw/memory.py`); demo in `examples/interface/aximm_queue_demo.py`.
  - RF part constants: `RFSOC4X2_PART` and the 4 ns clock in `waveflow/build/composite_gen.py`.
  - Matrix-multiply option: `waveflow.vitis_l1.gemv` (real only).
  - Example-level ignore pattern: `examples/stream_inband/.gitignore`.
- **Conventions to follow:**
  - `CLAUDE.md`.
  - Example layout: `examples/<name>/<name>.py` (model), `<name>_build.py` (DAG via `run_dag_cli`), tests in `tests/examples/`.
  - Markers `vitis` and `xsi` for toolchain tests, and a loud skip when prerequisites are missing (see `tests/examples/test_xsi_bfm.py::_require`).
  - Plans carry a Status line.
  - Docs pages under `docs/examples/<name>/` with `images/`.
  - Numbers stated in docs may be machine-checked by `tests/docs/test_documented_numbers.py`; read it before documenting numbers.
  - numpy-style docstrings; ruff/black formatting for new files.
- **Docs to keep in sync:**
  - `docs/examples/mimo_cg/` (new) and `docs/examples/index.md` (add the example).
  - `docs/guide/schema/python/fixpoint.md` (the new division op).
  - `plans/paper_cg_dse_vision.md` (status table).
  - `plans/cg.md` (superseded banner).

## 8. Tools and access (readiness)

Checked 2026-09-29. Re-run before starting:

```bash
# guided-task plugin's readiness script, via its marketplace_env (created 2026-09-29):
export MARKETPLACE_ROOT=/home/wirelesslab914/ali/claude-marketplace
. "$MARKETPLACE_ROOT/common/scripts/skill_env.sh"
READY="$MARKETPLACE_ROOT/common/plugins/guided-task/skills/guided-task/scripts/check_readiness.py"
cd /home/wirelesslab914/ali/waveflow && source .venv/bin/activate
"$PY" "$READY" --timeout 180 \
  --tool git --tool g++ \
  --tool "$PWD/.venv/bin/python" \
  --tool /tools/Xilinx/Vitis/2024.1/bin/vitis-run \
  --tool "/tools/Xilinx/Vivado/2024.1/bin/vivado=-version" \
  --tool /tools/Xilinx/Vivado/2024.1/bin/xelab \
  --env XILINX_VIVADO --env LD_LIBRARY_PATH \
  --path plans/paper_cg_dse_vision.md --path plans/cg.md --path examples/vmac \
  --path waveflow/hw/complexfield.py --path waveflow/hw/fixpoint.py --path waveflow/build/sweep.py \
  --path waveflow/calib/device_rules.py \
  --path .venv/xsi_compat/libxv_simulator_kernel.so \
  --path /tools/Xilinx/Vivado/2024.1/data/parts/xilinx/zynquplusRFSOC \
  --writable plans --writable examples/mimo_cg --writable tests/examples \
  --url https://docs.amd.com \
  --git .
```

| Check | Target | Status | Detail |
|---|---|---|---|
| tool | `git` | ✅ ready | git version 2.43.0 (/usr/bin/git) |
| tool | `g++` | ✅ ready | g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0 (/usr/bin/g++) |
| tool | `/home/wirelesslab914/ali/waveflow/.venv/bin/python` | ✅ ready | Python 3.12.3 (~/ali/waveflow/.venv/bin/python) |
| tool | `/tools/Xilinx/Vitis/2024.1/bin/vitis-run` | ✅ ready | ****** vitis-run v2024.1 (64-bit) (/tools/Xilinx/Vitis/2024.1/bin/vitis-run) |
| tool | `/tools/Xilinx/Vivado/2024.1/bin/vivado` | ✅ ready | vivado v2024.1 (64-bit) (/tools/Xilinx/Vivado/2024.1/bin/vivado) |
| tool | `/tools/Xilinx/Vivado/2024.1/bin/xelab` | ✅ ready | Vivado Simulator v2024.1 (/tools/Xilinx/Vivado/2024.1/bin/xelab) |
| env | `XILINX_VIVADO` | ✅ ready | set (value not shown; presence only, not validity) |
| env | `LD_LIBRARY_PATH` | ✅ ready | set (value not shown; presence only, not validity) |
| path | `plans/paper_cg_dse_vision.md` | ✅ ready | file, readable |
| path | `plans/cg.md` | ✅ ready | file, readable |
| path | `examples/vmac` | ✅ ready | directory, readable |
| path | `waveflow/hw/complexfield.py` | ✅ ready | file, readable |
| path | `waveflow/hw/fixpoint.py` | ✅ ready | file, readable |
| path | `waveflow/build/sweep.py` | ✅ ready | file, readable |
| path | `waveflow/calib/device_rules.py` | ✅ ready | file, readable |
| path | `.venv/xsi_compat/libxv_simulator_kernel.so` | ✅ ready | file, readable |
| path | `/tools/Xilinx/Vivado/2024.1/data/parts/xilinx/zynquplusRFSOC` | ✅ ready | directory, readable |
| writable | `plans` | ✅ ready | directory, writable |
| writable | `examples/mimo_cg` | ✅ ready | does not exist yet — will be created (parent is writable) |
| writable | `tests/examples` | ✅ ready | directory, writable |
| url | `https://docs.amd.com` | ✅ ready | HTTP 200 |
| git | `.` | ⚠️ check | branch `main`: 1 uncommitted change(s) — commit or stash them to create a checkpoint before starting |

**Readiness: READY** — 21 ready · 1 to check · 0 blocked

**The xczu48dr probe (approved 2026-09-29).** It was run on 2026-09-29 in a
scratch directory. Step 0.2 commits these two files verbatim to
`examples/mimo_cg/tools/xczu48dr_probe/` and re-runs them there, where
`examples/mimo_cg/.gitignore` keeps `probe_proj/` out of git.

`probe.cpp`:

```cpp
#include <ap_fixed.h>
#include <complex>
typedef ap_fixed<16, 2> fx_t;
typedef ap_fixed<40, 10> acc_t;
// Complex MAC over 8 elements: the inner op of a CG dot product.
void probe_cmac(const std::complex<fx_t> a[8], const std::complex<fx_t> b[8], std::complex<acc_t>* y) {
#pragma HLS PIPELINE II=1
    acc_t re = 0, im = 0;
    for (int i = 0; i < 8; i++) {
        re += a[i].real() * b[i].real() - a[i].imag() * b[i].imag();
        im += a[i].real() * b[i].imag() + a[i].imag() * b[i].real();
    }
    *y = std::complex<acc_t>(re, im);
}
```

`probe.tcl`:

```tcl
open_project -reset probe_proj
set_top probe_cmac
add_files probe.cpp
open_solution -reset solution1
set_part {xczu48dr-ffvg1517-2-e}
create_clock -period 4
if {[catch {csynth_design} res]} { puts "PROBE_ERROR: $res"; exit 1 }
puts "PROBE_CSYNTH_OK"
exit 0
```

Run it with `cd examples/mimo_cg/tools/xczu48dr_probe && /tools/Xilinx/Vitis/2024.1/bin/vitis-run --mode hls --tcl probe.tcl`.

The 2026-09-29 result:
- `exit=0`, `PROBE_CSYNTH_OK`, and no licence messages in the log.
- Utilization total: 0 BRAM, **32 DSP**, 1431 FF, 805 LUT, which is 4 DSPs per 16-bit complex multiply.
- The loop pipelined at interval 4, with latency 8 cycles (32 ns).

| Capability | Kind | Purpose | Available? |
|---|---|---|---|
| Vitis HLS 2024.1 (`vitis-run`) | tool | csim, csynth, co-simulation | ✅ |
| Vivado / xsim 2024.1 | tool | XSI RTL checks | ✅ (needs the activated venv) |
| csynth for `xczu48dr-ffvg1517-2-e` | device support | all hardware phases | ✅ probe passed |
| Vivado implementation for `xczu48dr` | licence | only if post-implementation numbers are wanted (gate 5.0) | ⚠️ untested; licence file present, not opened |
| AMD documentation (docs.amd.com) | web | authoritative `ap_fixed` and HLS semantics | ✅ HTTP 200; web access approved |
| Explore / general-purpose / Plan subagents | agent | broad code reading, independent review | ✅ in this Claude Code setup |
| `/code-review` skill | skill | milestone diff review | ✅ |
| guided-task plugin | plugin | this plan's process and readiness gate | ✅ `marketplace_env` created 2026-09-29 |
| The user | person | decisions at every 👁 gate | ✅ |
| Amir Reza Kiani | person | owner of `plans/vitis_l1_hwmodule.md` (matrix-multiply overlap) | consult at gate 4.0 |

**Gaps the user accepted:**
- **The `.gitignore` edit stays uncommitted.** Workaround: stage files by explicit path only. Risk: an accidental `git add -A` would commit it.
- **Vivado implementation on xczu48dr is unverified.** It isn't needed before gate 5.0, and gate 5.0 probes it before relying on it.

## 9. Approach

**Chosen approach:** follow the vision's experimental structure, accuracy half
first.
1. Build and validate a floating-point link simulator (Phase 1).
2. Make the detector bit-exact and prove it against Vitis C-simulation (Phase 2).
3. Run the accuracy DSE, which needs no Vitis (Phase 3).
4. Build the hardware blocks (Phase 4), calibrate per-block performance models (Phase 5), and close with the full DSE and brute-force baseline (Phase 6).

This order gets a candidate design finding early and cheaply. It lets the
accuracy results constrain which hardware configurations are worth building,
and it touches Vitis early only where bit-exactness must be proven (Phase 2).
Phases 4–6 are milestone-level on purpose. Their steps depend on the Phase 2
decisions and are refined, with the user's approval, at gates 4.0, 5.0 and 6.0.

**Search before building:**
- The Waveflow pieces in §7 are reused, including the existing fixed-point conformance harness for division.
- For the link simulator, the external options were Sionna and scikit-commpy. Sionna pulls in TensorFlow, which is heavy and has no role in this repo. scikit-commpy would add an unvetted dependency for about 150 lines of numpy. The choice: write the QAM mapper, channel, noise and BER counting in numpy under `examples/mimo_cg/`, and validate them against closed-form theory (AC1.1, AC1.2).
- No existing CG, MIMO or wireless code exists in the repo.

**The corrected CG.** The `plans/cg.md` sketch has `X - P*alpha` where it needs
`X + P*alpha`, and `R - P*beta` where it needs `R + P*beta`. Its `rnorm = rnorm`
is a no-op, and it starts from a 1-D `X`. The corrected multi-RHS CG solves
`A X = B`, with A Hermitian positive definite, independently for every column;
`B = I` gives the inverse and `B = HᴴY` gives the detector outputs directly.
This is the §AC0.2 check script, verbatim (run with `python -`):

```python
import numpy as np

def cg_multi_rhs(A, B, nit, explicit_residual=False):
    X = np.zeros_like(B); R = B.copy(); P = R.copy()
    rnorm = np.sum(np.abs(R) ** 2, axis=0)            # column norms, real
    for _ in range(nit):
        S = A @ P                                      # the matrix-multiply block
        ps = np.real(np.sum(np.conj(P) * S, axis=0))   # column dots, real for Hermitian PD A
        alpha = rnorm / ps                             # division (Phase 2 decision; fixed point needs a zero guard)
        X = X + P * alpha
        R = B - A @ X if explicit_residual else R - S * alpha   # recurrence: one matmul per iteration
        rnorm_new = np.sum(np.abs(R) ** 2, axis=0)
        beta = rnorm_new / rnorm
        rnorm = rnorm_new
        P = R + P * beta
    return X

rng = np.random.default_rng(0)
H = (rng.standard_normal((64, 8)) + 1j * rng.standard_normal((64, 8))) / np.sqrt(2)
A = H.conj().T @ H + 0.1 * np.eye(8)
Ainv = np.linalg.inv(A)
for nit in (2, 4, 6, 8):
    err = np.linalg.norm(cg_multi_rhs(A, np.eye(8, dtype=complex), nit) - Ainv) / np.linalg.norm(Ainv)
    print(nit, f"{err:.3g}")
assert err <= 1e-10
```

At planning (2026-09-28) this printed relative errors of 9.7e-2, 4.8e-3, 1.1e-4
and 2.0e-16 for 2, 4, 6 and 8 iterations (default recurrence form). The sketch as written stays near 1.5.

**The theory points in AC1.1 and AC1.2** were computed at planning:
- AWGN points: the exact Gray square-QAM BER of Cho and Yoon (2002) at Es/N0 = ρ.
- ZF points: the MRC closed form with L = M−K+1 branches and per-bit SNR ρ/2. It matches a numerical integration of the QPSK BER over ρ·Gamma(17, 1) to four digits.

The analytical ZF BER = 1e-3 crossings, used for the Phase 3 SNR windows, were
computed by the same integration with the exact QAM BER:

| Modulation | 32×4 | 32×8 | 32×16 | 64×4 | 64×8 | 64×16 | 128×4 | 128×8 | 128×16 |
|---|---|---|---|---|---|---|---|---|---|
| QPSK (dB) | −4.4 | −3.7 | −1.8 | −7.9 | −7.6 | −6.9 | −11.1 | −10.9 | −10.6 |
| 16-QAM (dB) | 2.3 | 3.0 | 4.9 | −1.1 | −0.8 | −0.1 | −4.3 | −4.2 | −3.9 |
| 64-QAM (dB) | 8.3 | 9.0 | 10.9 | 4.9 | 5.2 | 5.9 | 1.7 | 1.8 | 2.1 |

Step 1.4 recomputes this table in code, as `paper_data/zf_crossings.csv`, and
checks it against these values to ±0.1 dB.

Alternatives considered:

**(a) Overall order**

| Option | Pros | Cons | Fits the requirements? |
|---|---|---|---|
| Accuracy first (chosen) | No Vitis until Phase 2; early finding; accuracy results prune the hardware space | Integration risk surfaces later | ✅ matches the vision's structure |
| Hardware first | Performance numbers early | Builds blocks before knowing which widths matter; Vitis-heavy from day one | ⚠️ |
| Vertical slice (one tiny configuration end to end first) | Surfaces integration risk early | Throwaway work before the design decisions settle | ⚠️ partly covered by AC4's small-configuration RTL check |

**(b) Division for α and β** (decided at gate 2.1). Whatever the choice, the
fixed-point version needs a defined zero-divisor behaviour: `ps` or `rnorm` is
exactly zero once a column's residual quantizes to zero, and `ap_fixed`
division by zero is undefined. The recommended guard is α = 0 or β = 0 for that
column (the column freezes), in both Python and C++.

| Option | Pros | Cons | Fits the requirements? |
|---|---|---|---|
| `ap_fixed` native division | Simplest to model bit-exactly; one operator; joins `render_binop("/")` | HLS dividers are long-latency and LUT-heavy | ✅ recommended starting point |
| Reciprocal table plus Newton–Raphson | Cheap, DSP-based; iteration count becomes a DSE axis | More ops to model; table initialization choice | ✅ good second option, or an extra DSE axis |
| Floating-point scalars (float32 α, β) | IEEE division is correctly rounded, so numpy float32 can be bit-exact | Mixes number systems; float cost on FPGA | ⚠️ keep as fallback |

**(c) Matrix-multiply block** (decided at gate 4.0)

| Option | Pros | Cons | Fits the requirements? |
|---|---|---|---|
| New systolic HwModule (array size a parameter) | What the vision names; the matrix-matrix `A @ P` of multi-RHS CG maps naturally | New code; nothing systolic exists yet | ✅ recommended |
| Wrap `waveflow.vitis_l1.gemv` | Bit-exact model exists | Real-valued only (4 real GEMVs per complex); HwModule wrapper not built (`plans/vitis_l1_hwmodule.md` S1+ open, another owner) | ⚠️ |
| VMAC inner products only | No new block | Slow; weakens the architecture story | ❌ |

**(d) Vector unit** (decided at gate 4.0): extend VMAC with per-column α and
AXPY (recommended, least new code), rebuild it on the interleaver foundation as
its docs suggest, or write a new vector unit.

**(e) Cycle ground truth** (decided at gate 5.0): XSI BFM cycle counts, which
reproduced exactly on 2024.1, or co-simulation reports, whose AXI-Lite
transaction counting differs on 2024.1. XSI is recommended for multi-block
designs.

## 10. Steps

Phases 0–3 are step-level. Phases 4–6 are milestone-level and are refined into
steps at their entry gates (4.0, 5.0, 6.0), with the user approving the plan
update. Every step's checkpoint is a commit on `paper/mimo-cg`, by the user's
identity, with the trailer (Rules 9).

### Phase 0 — Baseline and bookkeeping (milestone M0)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 0.1 | Create branch `paper/mimo-cg` from `main`; create `plans/mimo_cg_lessons.md`; commit this plan and the lessons file | this plan | On the branch; last commit authored and committed by `ali-rasteh <ali.rasteh1@gmail.com>`, with the trailer; working tree shows only ` M .gitignore` (and the ignored `docs/repo_docs/`) | `git branch --show-current`; `git log -1 --format='%an <%ae> / %cn <%ce>%n%b'`; `git status --short` | commit | ☑ |
| 0.2 | Create `examples/mimo_cg/.gitignore` ignoring `gen/`, `include/`, `*_proj/`, `results/`, `logs/`. Commit the §8 probe sources verbatim to `examples/mimo_cg/tools/xczu48dr_probe/` and run the probe there. Run `test_amd_tools` (expect exit 1, "TOO OLD", 2024.1) | §8 | Probe prints `PROBE_CSYNTH_OK`; `git check-ignore -v examples/mimo_cg/tools/xczu48dr_probe/probe_proj` reports the example `.gitignore`; `git check-ignore examples/mimo_cg/paper_data/x.csv` reports nothing; both outputs pasted into §15 | the commands themselves | commit | ☑ |
| 0.3 | Re-check the existing bit-exact conformance on 2024.1: `pytest -m vitis -rs tests/examples/test_fixedpoint_conformance.py tests/examples/test_complex_conformance.py tests/hw/test_fixpoint_vitis.py -q` | — | AC0.1: N passed, 0 skipped, or every failure root-caused, recorded in §14 and accepted by the user | pytest summary with `-rs` | commit (§15, lessons) | ☐☑ |
| 0.4 👁 | Add a "Superseded" banner and the corrected algorithm (§9) to `plans/cg.md`; refresh the build-vs-have table in `plans/paper_cg_dse_vision.md` from §7 and link this plan | §7, §9 | AC0.2 | run the §9 script with `.venv/bin/python -`; user reviews the diff | commit | ☐☑ |
| 0.5 | Regression baseline: `pytest -m "not vitis and not xsi" -p no:cacheprovider`; record the `ruff check --output-format concise` finding counts of `waveflow/hw/fixpoint.py`, `waveflow/utils/fixputils.py` and `waveflow/calib/device_rules.py` on `main` in §15 | — | Exactly the 7 known failures in §6; the three counts recorded | pytest summary; ruff output | commit (§15) | ☐☑ |

### Phase 1 — Floating-point link-level reference (milestone M1, no Vitis)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 1.1 | `examples/mimo_cg/mimo_link.py`: Gray QAM mapper/demapper (4/16/64), i.i.d. Rayleigh H (M×K, CN(0,1)), noise at σ² = 1/ρ, exact Cho–Yoon BER function, BER counting with Wilson intervals, per-point `SeedSequence` seeding; module docstring states the SNR convention | §2, §5 | AC1.1 | `pytest tests/examples/test_mimo_cg_link.py -q` | commit | ☐ |
| 1.2 | `examples/mimo_cg/detectors.py`: ZF, exact MMSE, plain CG, multi-RHS CG (`B = HᴴY`, `B = I`), recurrence and explicit-residual forms, `nit` parameter, optional Jacobi preconditioner (off by default); MRC closed form for the ZF check | 1.1, §9 | AC1.2 | `pytest tests/examples/test_mimo_cg_detectors.py -q` | commit | ☐ |
| 1.3 | `profile_ranges()` in `examples/mimo_cg/detectors.py`: max \|value\| per variable (A, P, S, R, X, ps, α, β, rnorm) per iteration, with and without normalizing A by M | 1.2 | A unit test on a fixed small case passes: each reported maximum is ≥ the value observed by directly instrumenting one CG run | `pytest tests/examples/test_mimo_cg_detectors.py -q -k ranges` | commit | ☐ |
| 1.4 | `examples/mimo_cg/mimo_cg_build.py` DAG via `run_dag_cli`. Step `float_ber`: BER vs SNR (−20 to +20 dB, 1 dB) for ZF, exact MMSE and CG with `nit` ∈ {1, 2, 3, 4, K} over all 27 (M, K, modulation) configurations; Monte Carlo stop rule per point: ≥ 100 bit errors or a 1e7-bit budget. Step `zf_crossings`: the analytical crossing table. Step `float_ranges`: 1.3 over the scenarios. Step `float_figures`: the SVGs. If a serial run exceeds 2 h, parallelize the Monte Carlo inside the step (multiprocessing, 8 workers); per-point seeding keeps results identical | 1.1–1.3 | AC1.3; `zf_crossings.csv` matches the §9 table to ±0.1 dB; every configuration's MMSE curve crosses 1e-3 inside −20…+20 dB | the build command twice with `--force`; `sha256sum`; a pytest comparing `zf_crossings.csv` with the §9 table | commit | ☐ |
| 1.5 👁 | M1 review: the user checks the curves (sanity: at M/K = 8, CG approaches exact MMSE within a few iterations, as reported in the literature) | 1.4 figures | User approves | — | commit, pause | ☐ |

### Phase 2 — Bit-exact fixed-point CG (milestone M2)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 2.1 👁 | Decision gate: division method and zero-divisor guard (§9 b); CG variant for hardware (`B = HᴴY` vs `B = I`; recurrence vs explicit residual; multi-RHS vs true block-CG); A normalization; per-variable formats from 1.3; wide formats with ≥ 24 fraction bits that respect `FixedField`'s 64-bit cap | 1.3, 1.4, UG1399 | AC2.1 | user approval | commit | ☐ |
| 2.2 | Implement the chosen real-valued division or reciprocal in `waveflow/hw/fixpoint.py` / `waveflow/utils/fixputils.py`, matching `ap_fixed` semantics (read UG1399 2024.1 first). Add it to the existing harness (`render_binop("/", …)` in `examples/schemas/fixedpoint/fixedpoint_build.py`) and tests (`tests/hw/test_fixpoint.py`, `tests/hw/test_fixpoint_vitis.py`), including the zero-divisor case; document it in `docs/guide/schema/python/fixpoint.md`. Only real division is needed: `ps` and `rnorm` are real | 2.1 | AC2.2; AC-R lint rule for the changed files | `pytest tests/hw/test_fixpoint.py -q`; `pytest -m vitis -rs tests/hw/test_fixpoint_vitis.py -q`; ruff counts vs step 0.5 | commit 👁 (framework change) | ☐ |
| 2.3 | `examples/mimo_cg/mimo_cg_fixed.py`: the chosen CG on `DataArray[ComplexField]` with a formats dataclass and the zero guard; returns stored integers and real views | 2.1, 2.2 | First half of AC2.4: X within relative 1e-4 of float CG at every `nit` ≤ K, M = 64, K = 8, ≥ 20 seeds | `pytest tests/examples/test_mimo_cg_fixed.py -q -k tracks_float` | commit | ☐ |
| 2.4 | `examples/mimo_cg/cpp/`: C++ reference of the same CG (`ap_fixed`, `std::complex`, the repo's C++ utils), driven by a conformance harness mirroring `examples/schemas/fixedpoint/fixedpoint_build.py` (read how it produces its C++ side before writing any); part `xczu48dr-ffvg1517-2-e`; cases include a zero-residual column | 2.3 | AC2.3 | `pytest -m vitis -rs tests/examples/test_mimo_cg_conformance.py -q` | commit | ☐ |
| 2.5 | Plug the bit-exact detector into the link simulator | 2.3 | Second half of AC2.4 (BER inside the float CG interval at 3 SNR points) | `pytest tests/examples/test_mimo_cg_fixed.py -q` | commit | ☐ |
| 2.6 👁 | M2 review | 2.1–2.5 | User approves | — | commit, pause | ☐ |

### Phase 3 — Accuracy DSE, no Vitis (milestone M3)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 3.1 👁 | Define the grid: datapath width W (proposed {8, 10, 12, 14, 16, 18, 20}), guard bits, `nit` (proposed {1, 2, 3, 4, 6, 8, 12, 16}, kept only where `nit ≤ K` via `Stage.when`), (M, K) over {32, 64, 128} × {4, 8, 16}, modulation. SNR: 13 values, the configuration's ZF crossing ±6 dB in 1 dB steps (§9 table). Estimate runtime from a timed sample; if serial runtime exceeds 12 h, parallelize the Monte Carlo inside the stage (8 workers) | Phase 2 | Grid, kept-point count and runtime estimate in §14, approved | the estimate script's output | commit | ☐ |
| 3.2 | `examples/mimo_cg/mimo_cg_accuracy_sweep.py`: `ParamGrid` + `Stage(when=…)` + `SweepRunner` + `sweep_cli`. The stage's DAG step writes each point's 13 rows to `results/accuracy_points/<point>.csv`; a merge step writes `paper_data/accuracy_grid.csv` sorted by the grid keys. Plus `tests/examples/test_mimo_cg_sweep_no_vitis.py` | 3.1 | Dry run prints the approved kept-point count; the no-Vitis test passes | `python -m examples.mimo_cg.mimo_cg_accuracy_sweep --dry-run --out results/dry_run.json`; `pytest tests/examples/test_mimo_cg_sweep_no_vitis.py -q` | commit | ☐ |
| 3.3 ⚠️ cost | Run the sweep (approval at start if the estimate exceeds 2 h), then the merge | 3.2 | AC3.1 | row count vs kept points × 13; the no-Vitis test | commit (CSV) | ☐ |
| 3.4 | Analysis and figures: SNR loss at BER 1e-3 against float exact MMSE (interpolated in log-BER), with `floor` for designs that never reach it; the smallest (W, `nit`) per (M, K, modulation) with loss ≤ 0.5 dB; BER families and loss heatmaps | 3.3 | AC3.2 | the analysis command twice with `--force`; `sha256sum` | commit | ☐ |
| 3.5 👁 | M3 review: write the candidate finding in §14 | 3.4 | AC3.3 | user approval | commit, pause | ☐ |

### Phase 4 — Hardware blocks on xczu48dr (milestone M4, refined at gate 4.0)

- **4.0 👁 Entry gate:**
  - Decide the matrix-multiply block (§9 c) and the vector unit (§9 d).
  - Decide the shared-memory and queue layout.
  - Pick the configurations to build from the Phase 3 frontier.
  - Check `plans/vitis_l1_hwmodule.md` with its owner. If GEMV is chosen, first re-verify it on 2024.1 with its Vitis flow (`tests/vitis_l1/gemv/verifyGEMV/`, see `tests/vitis_l1/gemv/VERIFY.md`); `pytest tests/vitis_l1` alone never runs Vitis.
  - Rewrite 4.a–4.c into step rows with named tests, and have the user approve the plan update.
- **4.a Vector unit:** CG column dots (`ps`, `rnorm`), the α/β scalar ops using the 2.2 division and zero guard, and the AXPY updates of X, R and P. Gates: bit-exact to the Phase 2 golden in Python simulation, csynth at 4 ns, bit-exact at RTL.
- **4.b Matrix-multiply block:** array size as a parameter, with the same three gates.
- **4.c CG control with shared memory and queue:** the integrated detector, bit-exact in Python simulation and at RTL on at least M = 32, K = 4 (AC4).
- **Pause** after M4.

### Phase 5 — Performance models (milestone M5, refined at gate 5.0)

- **5.0 👁 Entry gate:**
  - Decide the cycle ground truth (§9 e).
  - Decide the calibration grid per block, and write `paper_data/holdout_split.csv` before any fitting (AC5 minimum counts).
  - Confirm or tighten the AC5 targets.
  - Decide whether post-implementation numbers are wanted. If they are, probe Vivado implementation on xczu48dr first; that needs the licence, which is so far unverified.
  - Refine the steps.
- **5.a Resource corpus and platform:**
  - Per-block resource corpus on xczu48dr via `SweepRunner` and `InspectSynthStep`, into a new platform at `examples/mimo_cg/calib/platforms/xczu48dr_250mhz/` (`waveflow_calib new` / `publish`).
  - Add a complex-multiply DSP rule to `waveflow/calib/device_rules.py`, starting from the probe's 4 DSPs per 16-bit complex multiply and checked across widths (👁, framework change; AC-R lint rule).
- **5.b Per-block cycle models:** `TimingModel` / `LinCalibModel` fitted from the chosen ground truth.
- **5.c Full-design estimate:** `compose`, plus an integration term fitted from a handful of full-design runs, none of them held out.
- **5.d Held-out validation:** `paper_data/model_validation.csv` plus a figure (AC5).
- **Pause** after M5.

### Phase 6 — Full DSE, baseline and finding (milestone M6, refined at gate 6.0)

- **6.0 👁 Entry gate:**
  - Fix the full grid and the held-out brute-force subset within the 48-hour budget.
  - Confirm or tighten the AC6 fidelity measure.
  - Decide whether to run the sampling experiment (6.c).
  - Refine the steps.
- **6.a Full cross-product DSE in Python:** exact accuracy plus predicted resources and cycles, giving the Pareto frontier.
- **6.b ⚠️ cost — Brute-force Vitis baseline:** on the held-out subset, with approval at start. Deliver the decision-fidelity comparison and the cost table.
- **6.c Optional sampling experiment:** active or decision-aware sampling, measured as K syntheses against frontier stability.
- **6.d Finish:**
  - The design finding, final figures and `paper_data/`.
  - `docs/examples/mimo_cg/` pages and a `docs/examples/index.md` entry.
  - Refresh the `plans/paper_cg_dse_vision.md` status.
  - Propose `CLAUDE.md` additions (the 2024.1 and venv notes) for the user to approve.
- Then the §16 close-out.

**Milestones:** M0 = 0.1–0.5 · M1 = 1.1–1.5 · M2 = 2.1–2.6 · M3 = 3.1–3.5 · M4 = 4.0–4.c · M5 = 5.0–5.d · M6 = 6.0–6.d. The user reviews at the end of each.

**Flags:** ⚠️ irreversible or costly (see §12) · 👁 the user reviews before continuing.

## 11. Verification strategy

- **Per step:** the *Verify with* column. The strongest available checks are:
  - exact closed-form theory for the simulator (AC1.1, AC1.2);
  - bit-for-bit comparison against Vitis 2024.1 C-simulation for anything claimed bit-exact, counted only when it ran (Rules 11);
  - RTL (co-simulation or XSI) for hardware blocks;
  - held-out comparison, with the split fixed before fitting, for models;
  - forced (`--force`), byte-identical re-runs for every data file and figure.
- **Regression check:**
  - `pytest -m "not vitis and not xsi" -p no:cacheprovider`, expecting only the 7 pre-existing failures in §6.
  - From Phase 2 on, also `pytest -m vitis -rs tests/examples/test_mimo_cg_conformance.py tests/hw/test_fixpoint_vitis.py tests/examples/test_fixedpoint_conformance.py tests/examples/test_complex_conformance.py -q`, with 0 skipped.
  - The AC-R lint rule.
- **Independent review:**
  - The plan was reviewed by an independent reviewer subagent on 2026-09-29, and all its findings were applied (§15).
  - Each milestone's diff gets `/code-review`, or a general-purpose reviewer agent (the L3 recommendation; confirm in execute mode).

## 12. Safety

- **Version control:** branch `paper/mimo-cg` from `main`, one commit per step. Commits are the user's (`ali-rasteh <ali.rasteh1@gmail.com>`) and carry the `Co-Authored-By:` trailer (Rules 9).
- **Diff review by the user:**
  - framework changes under `waveflow/` (`fixpoint.py`, `fixputils.py`, `device_rules.py`);
  - the shared conformance harness (`examples/schemas/fixedpoint/`, `tests/hw/test_fixpoint*.py`);
  - any `pyproject.toml` or dependency change;
  - edits to other plans (`plans/cg.md`, `plans/paper_cg_dse_vision.md`);
  - the docs index.
- **Irreversible or destructive actions:** none expected; git can undo every change. Long runs are costly, not irreversible:

  | Action | Step | Backup / recovery | Dry run | Approval |
  |---|---|---|---|---|
  | Floating-point BER run expected to exceed 2 h | 1.4 | re-run is deterministic; resume by step | a single configuration first | at the time of the action |
  | Accuracy sweep expected to exceed 2 h | 3.3 | `--resume` continues after interruption | `--dry-run --out results/dry_run.json` | at the time of the action |
  | Brute-force Vitis baseline (up to about 48 h) | 6.b | `--resume`; the subset is fixed in §14 first | `--dry-run` with its own `--out` | at the time of the action |

- **Permissions:** read and write inside this repo only, plus the AMD tools' own state under the home directory. No installs outside `.venv/`, and none inside it without asking.
- **Secrets:** none needed.
- **Deviation policy:** investigate up to 2 tested hypotheses, then stop and report (L3 default; confirm in execute mode). If the plan itself turns out wrong, propose the change, and update §10 and §14 once the user approves it.

## 13. Agents and models

| Role | Agent / model | Responsibility |
|---|---|---|
| Doer | Claude Code session running `/guided-task execute plans/mimo_cg_paper_sims.md` | Executes steps, verifies, logs |
| Explorer | Explore subagent | Broad read-only searches (e.g. VMAC internals, `vitis_l1` wrapper status) when a step needs them |
| Reviewer | Independent general-purpose subagent; `/code-review` for diffs | Plan review before approval (done 2026-09-29); diff review at each milestone |
| Decision maker | The user | Every 👁 gate and ⚠️ cost approval |

## 14. Decisions, assumptions, risks, open questions

| Kind | Item | Resolution / owner |
|---|---|---|
| Decision | Complexity L3 Complex | user, 2026-09-29 |
| Decision | Target part `xczu48dr-ffvg1517-2-e` at 4 ns. Alternatives were xc7z020 (calibrated platform exists, but only 220 DSPs caps the array sweep) and both parts. Chosen for 4272 DSPs and wireless credibility | user, 2026-09-29 |
| Decision | Vitis/Vivado 2024.1 for every paper number (installed); baselines re-checked in Phase 0. Alternative: install 2025.1 | user, 2026-09-29 |
| Decision | All phases planned; 0–3 step-level, 4–6 milestone-level with entry gates | user, 2026-09-29 |
| Decision | Scenario defaults in §2 (uplink, i.i.d. Rayleigh, perfect CSI, uncoded, M/K/modulation sets, BER 1e-3; plain and multi-RHS CG simulated, hardware targets multi-RHS CG) | user, 2026-09-29 |
| Decision | Code in `examples/mimo_cg/`; generic division in `waveflow/hw/fixpoint.py` through the existing fixed-point harness; tests in `tests/examples/` and `tests/hw/`; docs in `docs/examples/mimo_cg/`; Phase 0 edits `plans/cg.md` and `plans/paper_cg_dse_vision.md` | user, 2026-09-29 |
| Decision | Branch `paper/mimo-cg`, commit per step, author and committer `ali-rasteh <ali.rasteh1@gmail.com>`, commit messages keep the `Co-Authored-By: Claude` trailer, `.gitignore` edit kept out, pause after each milestone, lessons in `plans/mimo_cg_lessons.md` | user, 2026-09-29 |
| Decision | No deadline; brute-force budget of about 48 h csynth | user, 2026-09-29 |
| Decision | guided-task `marketplace_env` created; xczu48dr csynth probe run (passed); web access to AMD docs allowed; independent plan review run | user, 2026-09-29 |
| Decision | All independent-review findings applied: SNR grids moved to −20…+20 dB with per-configuration Phase 3 windows; lint rule limited to new files and no new findings; sweep outputs via stage steps and a no-Vitis test; `--force` re-runs with per-point seeding; toolchain tests must run with 0 skipped; exact Cho–Yoon BER, 99.9% intervals, named test points; CG tolerances; "block-CG" renamed multi-RHS CG; zero-divisor guard; example `.gitignore`; step order fixed; probe and CG check script inlined; AC5 minimum counts; provisional AC6 measure; no scratch-directory exception needed | user, 2026-09-29 |
| Assumption | HLS csynth on xczu48dr needs no licence check (probe showed none); Vivado implementation may. | re-checked at gate 5.0 |
| Assumption | The repo's bit-exact conformance (proven on 2025.1) holds on 2024.1 | **confirmed** in step 0.3 (2026-09-30): 89 passed, 0 skipped |
| Assumption | Block resources are roughly additive for a fixed, memory-decoupled architecture (the vision's caveat) | tested by the integration term and full-design runs in 5.c |
| Risk | Fixed-point division dominates latency or LUTs | reciprocal/Newton option (§9 b); division method can become a DSE axis |
| Risk | DSP cost: 4 DSPs per 16-bit complex multiply (probe) limits array sizes | size the grid from the Phase 3 frontier; consider 3-multiply complex products at gate 4.0 |
| Risk | `FixedField`'s 64-bit cap constrains wide formats and dot-product accumulators | choose wide formats within the cap at gate 2.1; quantize after multiply where needed |
| Risk | Monte Carlo runtime for low-BER points | stop rule (≥ 100 errors or 1e7 bits); parallelize inside the step (1.4, 3.1) |
| Risk | 2024.1 co-simulation cycle counting differs (regmap: 49 vs 5) | cycle ground-truth decision at gate 5.0; XSI reproduced exactly |
| Risk | Overlap with `plans/vitis_l1_hwmodule.md` (Amir Reza Kiani) and the planned VMAC rebuild | coordinate at gate 4.0 |
| Risk | Broad `.gitignore` rules silently drop committed data (`*.json`, `*summary*`, `hls/`) | CSV only, `cpp/` not `hls/`, `git check-ignore -v` before relying on a path |
| Risk | Held-out model accuracy misses the AC5 targets | vision's per-block + physics-prior approach; decide at 5.0 whether to densify or relax, with a §14 record |
| Decision | Execute mode: deviation policy confirmed as 2 tested hypotheses, then stop; an independent reviewer agent checks each milestone against its acceptance criteria | user, 2026-09-30 |
| Open question | Step-level detail for Phases 4–6 | refined at gates 4.0, 5.0, 6.0 |

## 15. Progress log

| Date | Step | What happened | Evidence | Deviation |
|---|---|---|---|---|
| 2026-09-29 | planning | Readiness gate READY (21 ✅, 1 ⚠️ dirty tree accepted); xczu48dr csynth probe passed | §8 | none |
| 2026-09-29 | planning | Independent review: 2 blocking and 12 should-fix findings plus 3 suggestions; all applied (§14) | this revision | none |
| 2026-09-29 | planning | User approved the written plan; status set to Ready | user approval | none |
| 2026-09-30 | gate | Readiness re-run: READY (21 ✅, 1 ⚠️ the accepted `.gitignore` edit); user said Start | §8 command, exit 0 | none |
| 2026-09-30 | 0.1 | The user had already created `paper/mimo-cg` from `main` (`b3d0487`) and committed this plan as `c04c029`; the doer added `plans/mimo_cg_lessons.md` (seeded with the setup and planning lessons) and set Status to In progress | this step's commit; `git log`, `git status` | branch and plan commit done by the user, not the doer |
| 2026-09-30 | 0.2 | Added `examples/mimo_cg/.gitignore` and committed the probe to `examples/mimo_cg/tools/xczu48dr_probe/`. Probe: `vitis-run v2024.1`, exit 0, `PROBE_CSYNTH_OK`, 0 licence messages; totals 0 BRAM, 32 DSP, 1431 FF, 805 LUT (same as planning). `test_amd_tools`: exit 1, Vitis and Vivado both `TOO OLD`, version 2024.1 (expected). `git check-ignore -v`: `probe_proj` and the Vitis-created `logs/` ignored by `examples/mimo_cg/.gitignore:5` and `:7`; `paper_data/x.csv` not ignored | the commands in step 0.2 | none |
| 2026-09-30 | 0.3 | `pytest -m vitis -rs -p no:cacheprovider tests/examples/test_fixedpoint_conformance.py tests/examples/test_complex_conformance.py tests/hw/test_fixpoint_vitis.py -q`: exit 0, **89 passed, 0 skipped, 0 failed** (34 + 51 + 4) in 9 min 16 s. The extra `-q` on top of pyproject's `-q` suppressed the summary line, so the count comes from the 89 `.` progress marks with no `s`, `F` or `E`. AC0.1 met; the conformance assumption is confirmed on 2024.1 | log of the run | none |
| 2026-09-30 | 0.4 | `plans/cg.md`: Superseded banner plus minimal fixes, keeping the explicit residual its architecture text describes. `plans/paper_cg_dse_vision.md`: build-vs-have table refreshed and this plan linked. §9 script: 9.66e-2, 4.75e-3, 1.05e-4, 2.01e-16 at nit 2, 4, 6, 8, assert passed. The `cg.md` code block extracted and run as written: 9.66e-2, 4.75e-3, 1.05e-4, 2.62e-16, assert passed. The user reviewed and approved the diff | both runs; user approval | none |
| 2026-09-30 | 0.5 | `pytest -m "not vitis and not xsi" -p no:cacheprovider`: 7 failed, 3360 passed, 37 skipped, 284 deselected in 83.8 s; the 7 failures are exactly those in §6. Baseline for AC-R, with `ruff 0.16.9`, `ruff check --output-format concise` (files identical to `main`): `waveflow/hw/fixpoint.py` 1, `waveflow/utils/fixputils.py` 0, `waveflow/calib/device_rules.py` 11 | pytest summary; ruff counts | none |

## 16. Completion report

*Filled in when the work ends.*

- **Changed:**
- **Tested (commands and results):**
- **Acceptance criteria:** AC0.1 … AC6, AC-R ✅/❌/⚠️ with evidence
- **Decisions made:**
- **Assumptions and not verified:**
- **Remaining risks and issues:**
- **Next steps:**
- **Reusable artifacts saved / tools that would have helped:**

## Checklist coverage

| Group | Addressed (where) | Not applicable (why) |
|---|---|---|
| T: task definition, planning | T1 §1–§4; T2 each step's *Verify with*; T3 header paths, §15, Rules 5; T4 §10 steps and milestones; T5 §9 alternatives, §14 | — |
| C: context and environment | C1 §5, §8; C2 §5 access column (all sources read directly); C3 §6; C4 `docs/repo_docs/` in §5; C5 Rules 2, §7; C6 §7 reuse and conventions | — |
| V: verification | V1 §10, §11, Rules 11; V2 §11 regression, AC-R; V3 §16, Rules 12; V4 Rules 8, §14; V5 Rules 4, §12 | — |
| D: documentation and continuity | D1 `plans/mimo_cg_lessons.md`, Rules 3; D2 step 6.d proposes `CLAUDE.md` additions; D3 §7 docs to sync, steps 0.4, 2.2, 6.d; D4 §16; D5 §16 reusable artifacts, the committed probe | — |
| R: version control and safety | R1 §12 branch and commits, Rules 9; R2 §12 diff review areas, 👁 flags; R3 §12 table | — |
| S: security and permissions | S2 §12 permissions | S1: no credentials are needed (the licence file is never opened) |
| A: authoritative sources | A1 §5 (UG1399 2024.1, Cho–Yoon 2002, MRC closed form), Rules 2 | — |
| P: plugins, MCP, tools | P1 §9 search before building; P2 §8 capabilities; P4 the doer runs Vitis/XSI itself (§8, §11); P5 §16 | P3: no MCP server is needed; every source is local or on docs.amd.com |
| M: multi-agent | M1 §11, §13 | — |
