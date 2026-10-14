# Plan: simulations for a design-space exploration of the long-term-beamforming CG solve

**Status:** In progress (approved by the user 2026-10-13 20:58 EDT; T0 = 2026-10-13 21:00 EDT)
**Complexity:** L3 Complex. A new workload two orders of magnitude larger than the existing linear-algebra units, ray-traced channels, fixed-point and hardware/software design axes, calibrated models with Vitis validation, under a hard 48-hour deadline; everything is undoable with git. Scores: scope 3 · clarity 3 · novelty 3 · dependencies 3 · verification 3 · risk 2
**Created:** 2026-10-13 · **Updated:** 2026-10-13 (revised after the independent plan review)
**Plan file:** `plans/mimo_cg/ltbf_cg_dse.md` · **Lessons learned:** `plans/mimo_cg/ltbf_cg_dse_lessons.md` (created in step 0.1)
**Branch:** `paper/cg_dse` (created in step 0.1 from `feat/cpu-model`)
**Deadline (hard):** **2026-10-15 20:25 EDT** (48 h after the user set it, 2026-10-13 ~20:25 EDT). All compute stops by **2026-10-15 18:30 EDT**; the completion report is written by **20:00 EDT**.

> **For the agent executing this plan:** read *Rules for the doer* first. The
> plan is self-contained. Paths are repo-relative, commands are exact, and
> secrets are named by environment variable only (this plan needs none).

## Rules for the doer

1. Before changing anything, re-run the readiness commands in §8. Stop if
   anything required is ❌ blocked. Run every Waveflow, pytest, Sionna or Vitis
   command inside the activated venv: `source .venv/bin/activate` (it also sets
   `XILINX_VIVADO` and `LD_LIBRARY_PATH`, which the XSI flow needs).
2. Do one step at a time (§10), except the background jobs, which run while the
   next steps proceed. A step is done only when its exit condition is verified
   with the stated method. Show the command and its result as evidence. Before
   changing code, read it and its callers. Check the authoritative docs for any
   version-sensitive API (versions in §6). Before building a tool, script or
   integration the plan didn't foresee, search for an existing one (start with
   `examples/mimo_cg/`, `waveflow/linalg/`, `waveflow/cpu/calib/`) and propose it first.
3. After each step, tick it in §10, add a line to the §15 progress log (with the
   wall-clock time), and append anything surprising to
   `plans/mimo_cg/ltbf_cg_dse_lessons.md`.
4. When something fails, find the root cause from the error, logs, tests and
   actual state. No random fixes. Deviation policy: investigate up to **2**
   tested hypotheses, then stop and report. **Never change a test's random seed,
   tolerance or confidence level to make it pass.**
5. **The deadline rules everything.** Scope is *full, best effort* (user,
   2026-10-13): follow the order of §10, keep each phase inside its time box
   (§10 *Time boxes*), and when a box is exhausted, stop that work, record what
   was and wasn't done in §15 and §14, and move on. The user pre-approved this
   cut rule. Never start a compute job whose estimated end is after
   **2026-10-15 18:30 EDT**. Tier-B work (§4) is cut before tier-A work.
6. **Pause only when required** (user, 2026-10-13): keep working through
   milestones and stop only for (a) a merge conflict other than the one
   pre-approved in step 0.2, (b) a 👁 gate, (c) a compute job that would exceed
   its budget in §2, (d) a change to `waveflow/` that §14 does not pre-approve,
   or (e) anything in §12. Batch the questions; while waiting, continue with
   work that doesn't depend on the answer. Reviewer agents run in the background
   and never block; their findings go to the user at the next stop.
7. **Long jobs run detached** (review B5; `mimo_cg_lessons.md`: a harness
   background task is killed after 30 min by default and 2 h at most, which
   killed an 8 h sweep). Every job longer than 30 min is launched as
   `setsid nohup <cmd> > examples/ltbf_cg/logs/<job>.log 2>&1 &`, is resumable
   per item (it skips finished items on restart), and has its PID and start time
   recorded in §15. Poll it with the Monitor tool (an until-loop on the log or a
   done-file) and long `ScheduleWakeup` fallbacks; never with a foreground sleep.
8. **CPU and memory discipline** (review S1). At most 8 busy workers in total.
   Every Python worker sets `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
   MKL_NUM_THREADS=1`; ray tracing sets Dr.Jit's thread count to its share of the
   cores. Large csynth runs (N_rx ≥ 256) run at most 2 at a time with a
   watchdog that kills a run above 10 GB of RSS. Run tracing with `nice -n 10`.
9. Never perform an action listed in §12 without its backup in place and fresh
   approval from the user at that moment, unless §14 records a pre-approval.
10. Never merge, push, deploy or publish unless the user asks. The one planned
    merge (step 0.2) is pre-approved for its known conflict only.
11. **No DSE data in git** (user, 2026-10-13): the copied Denver scene, traced
    drops, channel files, sweep outputs, CSVs, figures, logs and build trees stay
    local under the example's gitignored directories (step 0.3). Commit only
    code, tests, `examples/ltbf_cg/.gitignore`, the plan, the lessons file, and
    the step 0.2 merge commit (including the root `.gitignore` lines it brings,
    pre-approved). Pre-registered sets (step 4.5) are fixed by recording their
    SHA-256 in §15 and committing that record.
12. **No data from outside the repo** (user, 2026-10-13): the Denver scene is
    copied into the repo once (step 0.3) and only the copy is used. The paper
    repo `/home/wirelesslab914/ali/salsa-ltbf` is read-only and is read for its
    text and code (the algorithm's and simulation's source of truth), never as a
    data source and never imported.
13. **Commits** use the repo's effective identity (`ali-rasteh`); never pass
    `--author` or set `GIT_AUTHOR_*`/`GIT_COMMITTER_*`; end every message with
    `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage by explicit
    path, never `git add -A` or `git add .`. Never stage the root `.gitignore`
    (except through the step 0.2 merge) or `docs/repo_docs/`.
14. Name the tool version next to every measured number (Vitis/Vivado 2024.1,
    sionna-rt 2.2.0, gem5 v25.1). A criterion whose target is missed is ⚠️ in
    §16, never ✅, even if the number is reported.
15. Finish with §16: check every acceptance criterion, run the regression check,
    and write the completion report.

## 1. Goal

Produce the simulation evidence for a paper that is a **comprehensive design-space
exploration (DSE) of the conjugate-gradient solve of long-term beamforming (LTBF)**,
the algorithm of *Scalable Long-Term Beamforming for Massive Multi-User MIMO*
(`/home/wirelesslab914/ali/salsa-ltbf`, §III-B.1). The claim (user, 2026-10-13):
**find the cheapest RFSoC design (programmable logic plus the A53 processor) whose
LTBF projection stays within an SINR-loss budget of exact LTBF**, over algorithm axes
(iterations, fixed-point formats, explicit or factored covariance, regime, per-UE or
joint solve) and architecture axes (systolic array size, lanes, memory,
hardware/software split of the eigendecomposition), with calibrated cost models
validated against Vitis, and concrete design findings.

**The CG** (paper §III-B.1; code `salsa/salsa/utils.py:557` `cg_solve_complex`, called by
`salsa/salsa/salsa_cap.py:680`):

- **System.** `Q̂ = I + Σⱼ αⱼ ĤⱼĤⱼᴴ / N_SRS` (N_rx × N_rx, Hermitian positive definite,
  every eigenvalue ≥ 1); `Ĥⱼ ∈ ℂ^{N_rx × N̄}` the SRS snapshots of UE j, `N̄ = N_s·N_SRS = 8`.
  Equivalently `Q̂ = I + ĤαĤαᴴ`, `Ĥα = [√(α₁/N_SRS)Ĥ₁, …]` (paper eq. (Halpha)). In the code
  `αⱼ` is absorbed into the power-controlled channel, already scaled to unit noise
  (`ray_tracing.py:660–705`), so `Ĥα = Ĥ/√N_SRS`: **do not scale twice**.
- **Regime 1** (`Ĥᵢ` as right-hand side): solve `Q̂Aᵢ = Ĥᵢ` for the N̄ columns of each UE (or
  all jointly). **Regime 2** (`I` as right-hand side): solve `Q̂X = I` once, symmetrize
  `X ← (X + Xᴴ)/2`, then `Aᵢ = XĤᵢ`. The paper's code defaults to regime 1
  (`cg_rhs=True`, `salsa_cap.py:547`) even at 8×8, so its results are regime 1; regime 2 is a
  DSE axis here.
- **Recursion.** Multi-right-hand-side CG (the paper says "block CG", but each column has its
  own `ωⱼ`, `νⱼ`; no shared Krylov space), from `X₀ = 0`, `R₀ = D₀ = B`: `S = Q̂D`,
  `ωⱼ = ‖rⱼ‖²/(dⱼᴴsⱼ)`, `X += D·diag(ω)`, `R −= S·diag(ω)`, `νⱼ = ‖rⱼ'‖²/‖rⱼ‖²`,
  `D = R + D·diag(ν)`, for a fixed `t` (3–10 in the paper). The code stops a column when
  `dᴴs = 0` or `‖r‖² < 1e-10`. **Factored form:** `S = D + Ĥα(ĤαᴴD)` (exact).
- **After CG.** `Mᵢ = ĤᵢᴴAᵢ` (8×8), symmetrized, eigendecomposed; `Φᵢ` its top-r
  eigenvectors; `Gᵢ` has rows `(Aᵢφⱼ)ᴴ` (`salsa_cap.py:675–687`; r = 2, r = 1 in the paper's
  complexity table).

**The simulation the paper actually ran** (its code, and its own deviation log
`salsa-ltbf/docs/known_deviations.md`; review B1, B2):

- **Three sectors of 10 UEs each** (30 UEs; `ray_tracing.py:249`). Each sector's `Q̂` sums
  **every connected UE** (`salsa_cap.py:576–599`), so `Ĥα` has up to 8 × 30 = **240 columns**;
  the right-hand sides per sector are its own UEs' (about 80). Three sector solves per T_LT.
  (The paper's text says N̄·N_UE = 80 columns; this plan follows the code, user 2026-10-13.)
- **A sector nuller** `P_v` is applied to each sector's channel before the SRS draw
  (`salsa_cap.py:585–588`); the SRS uses 8 random distinct subcarriers per UE, the same for all
  sectors, with independent estimation noise per sector (`draw_srs`).
- **Two traces per drop:** the long-term statistics at the drop, the SINR one T_LT later after
  `move_users(vel·t_ltbf)` and a retrace (`salsa_cap.py:920–945`); only UEs connected in both
  are reported (the `Iserved` rule, D5).
- **Geometry and solver:** UEs on a `CoverageMapPlanner` grid excluding buildings
  (`ray_tracing.py:351–355, 480–490`), 100 m–2 km, 0–100 km/h; BS at 40 m, UEs at 1.5 m;
  tr38901 pattern, V polarization, sector yaws 0/120/240°, the UEs as transmitters
  (`ray_tracing.py:466–547`); `max_depth` 3, `samples_per_src` 1e5, `max_num_paths_per_src`
  100, no diffraction or diffuse reflection (`salsa_main.py:154–161`).
- **SINR:** per UE, `10·log10` of the frequency-mean of the linear per-stream MMSE SINR after
  projection (`salsa_cap.py:268–292, 968`).
- **Deviation log:** D12 (the evaluated channels are single-path, rank one for about 99% of
  UEs), D21 (the simulated band is `scs·n_sc_rb` = 1.44 MHz, not the 100 MHz the text says),
  D18/D20 (a multipath scenario needs scattering, and then tracing is not reproducible). The
  paper stores `Q̂`, `Ĥ` and `A` as complex64; this plan's reference is complex128.

**Choices made for this study** (user, 2026-10-13): the **100 MHz** span the paper's text
states (792 subcarriers at 120 kHz) as the primary band, which gives the channels the
frequency diversity that makes CG non-trivial, plus a **1.44 MHz** sensitivity run on the
same traces for fidelity to the code; the code's sector structure; no scattering scenario
(a stated limitation).

Writing the manuscript is out of scope. This plan delivers the code, the local data and
figures, and a findings summary.

## 2. Requirements and constraints

| Type | Item |
|---|---|
| Must | Scope: the **whole long-term update** per sector (factoring or forming `Q̂`, the CG solve in both regimes, `Mᵢ`, the 8×8 eigendecomposition, `Gᵢ`), three sectors per T_LT (user, 2026-10-13). The real-time per-RE path is out of scope. |
| Must | Channels: the paper's Denver scene, **copied into the repo** (`examples/ltbf_cg/data/denver/`, gitignored), ray-traced with `waveflow.rfenv` (Sionna RT 2.2.0, CPU; merged in step 0.2), with the paper's geometry and solver settings (§1), two snapshots per drop, specular-only (deterministic). |
| Must | Arrays: each drop traced once with the **32×32** BS array; **16×16 and 8×8 taken as sub-arrays** at the same spacing (paired drops; user, 2026-10-13). |
| Must | Scenarios: 3.5 GHz, 3 sectors × N_UE 10, N_SRS 8, N_s 1, G_SRS 10 dB (nominal) and noiseless SRS (for the paper comparison), SINR targets [−6, 3] and [−6, 14] dB, r ∈ {1, 2}, the 100 MHz band and the 1.44 MHz sensitivity; an N_UE sweep (4 to 40 per sector) at 16×16 for the regime and explicit/factored crossovers. Drops set by the step 1.2 spike within the tracing budget, aiming at 100, never below 20. |
| Must | Accuracy metric: per-UE SINR loss of an approximate (finite-t and/or fixed-point) LTBF projection against **exact float LTBF** (`Aᵢ = Q̂⁻¹Ĥᵢ` by a direct solve), as the median and the 10th-percentile loss over the reported UEs and drops; SINR-loss budgets 0.1, 0.25 and 0.5 dB. The Prop. 2 subspace error is a diagnostic. |
| Must | Arithmetic: a complex128 reference and a **bit-exact fixed-point** LTBF CG built on `waveflow.linalg`'s `matmul` and `cg` models, with word width (10–24 bits) and guard bits as axes; bit-exact to Vitis 2024.1 `ap_fixed`. |
| Must | Design axes. Algorithm: t ∈ {1…10}; word width; guard bits; `Q̂` **explicit** (`S = Q̂D`) or **factored** (`S = D + Ĥα(ĤαᴴD)`); regime; per-UE or joint solve (a hardware axis; accuracy-identical, §9 b). Architecture: systolic R×C, lanes L, number of units, on-chip memory, the eigendecomposition on the A53 or in the PL. |
| Must | Platform: RFSoC 4x2 `xczu48dr-ffvg1517-2-e` at 250 MHz (Vitis/Vivado 2024.1) plus the A53 model (`waveflow/cpu`, gem5 v25.1 HPI + McPAT) for the eigendecomposition and `Gᵢ`. |
| Must | HLS bodies at LTBF sizes are **example-level** (`examples/ltbf_cg/src/`), derived from `waveflow/linalg`'s; `waveflow/linalg` is not changed (gate 4.0 decided by the user, 2026-10-13: `SystolicCore` fully partitions its operand registers over `MMAX`, `systolic_core_task.h:106–145, 235–236`, so m = 1024 cannot build). |
| Must | Explicit `Q̂` built and measured **on chip up to the largest N_rx that fits** (about 256); N_rx = 1024 explicit is **modeled** (measured tile cost + a stated DDR-bandwidth model), flagged *model-only* (user, 2026-10-13). Factored `Q̂` measured at 64 and 256 (tier A) and 1024 (tier B; modeled from 64/256 if its csynth fails). |
| Must | Validation (user, reduced for the deadline): fit builds spanning the target sizes, **≥ 8 held-out** builds, a **pre-registered brute-force sub-grid** (≤ 8 h), **2–4 Vivado implementations** of finalists; each time-boxed. |
| Must | Every figure and table regenerated by one documented command with fixed seeds; a forced re-run of a pre-chosen subset reproduces it byte for byte (figures written with fixed metadata). |
| Must not | Commit the scene copy, drops, channel files, results, CSVs, figures, logs or build trees (user). |
| Must not | Read data from outside the repo (user); change anything outside the repo except the AMD tools' own state under `~/.Xilinx` and Docker's image store; open `~/.Xilinx/Xilinx.lic` or any credential file. |
| Must not | Push or merge unasked; change `feat/cpu-model` or `feat/sionna-prod`. |
| Must not | Change `waveflow/linalg` (gate 4.0) or `waveflow/rfenv` without a 👁 stop; change behaviour of any existing `waveflow/` API. Additions to `waveflow/cpu/calib` (pre-registration rows) are pre-approved (§14). |
| Must not | Reformat existing files; overwrite the repo's recorded gate numbers or calibration corpora (new corpora are local files; the A53 calibration uses a new local platform directory). |
| Nice to have | Jacobi (diagonal) preconditioning as an accuracy axis; warm start from the previous T_LT (the second snapshot gives one step of it); an eigendecomposition block in the PL; the paper's analytic ASIC operation-count model side by side. |
| Constraint | **Deadline 2026-10-15 20:25 EDT, hard.** Scope *full, best effort*, most important results first. |
| Constraint | Budgets, each capped by the deadline: tracing ≤ 8 h wall clock (≤ 4 cores); accuracy sweeps ≤ 8 h (≤ 4 workers); csynth fit + held-out + brute force ≤ 14 h in one queue (≤ 4 workers, ≤ 2 for N_rx ≥ 256); Vivado implementations ≤ 4; gem5 ≤ 1 h of compute. Disk: drops ≤ 45 GB (about 150 MB per 32×32 snapshot, because `rfenv` stores τ, Doppler and validity per antenna), build trees removed once their reports are harvested, ≥ 20 GB kept free. |
| Constraint | Python 3.12 `.venv`; Vitis HLS/Vivado 2024.1; sionna-rt 2.2.0 (Mitsuba 3.9.1, Dr.Jit 1.5.0, `llvm_ad_mono_polarized`); gem5 v25.1 via Docker; the repo's `CLAUDE.md`. 8 cores, 31 GB RAM (24 GB available), 108 GB free disk. |

## 3. Deliverables

| Deliverable | Format | Location | For whom |
|---|---|---|---|
| Branch with the code and tests | git | `paper/cg_dse` (committed) | the user, the paper |
| Drop generator, LTBF link model (float), bit-exact fixed-point LTBF CG | Python | `examples/ltbf_cg/` (committed) | paper, reproducibility |
| LTBF engines (factored and explicit `Q̂`), HLS bodies, harnesses, models, DSE | Python + C++ | `examples/ltbf_cg/hw/`, `examples/ltbf_cg/src/` (committed) | paper |
| A53 eigendecomposition kernel, `SwFunction`, calibration | Python + C | `examples/ltbf_cg/cpu/` (committed); pre-registration rows in `waveflow/cpu/calib/sweep.py` (pre-approved) | paper |
| Copied Denver scene, drops, channel files | XML, PLY, `.npz`/`.json` | `examples/ltbf_cg/data/` (**local**, gitignored) | the doer |
| Accuracy, cost, DSE, validation and fidelity tables; figures | CSV, PDF/PNG | `examples/ltbf_cg/paper_data/`, `examples/ltbf_cg/results/` (**local**) | the paper |
| Findings summary and figure index | Markdown | `examples/ltbf_cg/paper_data/FINDINGS.md` (**local**) and §16 (committed) | the user |
| Plan and lessons | Markdown | `plans/mimo_cg/` (committed) | continuity |

## 4. Acceptance criteria

Tier **A** criteria back the core claim and come first; tier **B** criteria are best effort,
cut first. A missed target is ⚠️ with the number, never ✅ (Rule 14).

- [ ] **AC0 (A) Branch and data hygiene.** `paper/cg_dse` exists from `feat/cpu-model` with `feat/sionna-prod` merged; `git ls-files examples/ltbf_cg | grep -E '(^|/)(data|paper_data|results|logs)/|\.(npz|csv|pdf|png)$'` prints nothing; `git rev-parse feat/cpu-model feat/sionna-prod` = `e0f12e19…`, `8ca01221…`. Verify: those commands.
- [ ] **AC1 (A) Drops.** ≥ 20 (target 100) drops, each two snapshots T_LT apart, traced at 32×32 from the repo-local scene copy with §1's geometry and solver settings (specular only); the 16×16 and 8×8 channels are sub-arrays of the same traces; re-tracing one drop from its seed reproduces its channel files bit for bit on this install. Verify: `pytest tests/examples/test_ltbf_cg.py -k drops` and the drop manifest.
- [ ] **AC2 (A) Float reference.** (a) CG to convergence (relative residual `‖rⱼ‖/‖bⱼ‖ < 1e-12` per column, t capped at 4·N_rx) matches the direct solve with relative error ≤ 1e-8 in `Aᵢ`, both regimes, explicit and factored `S`; (b) for the same `D`, explicit and factored `S` agree to ≤ 1e-12 relative; (c) on our drops, in regime 1 as the paper's code ran, the paper's statements are re-checked and the numbers printed in §15: exact LTBF against instantaneous MMSE with a noiseless SRS estimate, and t = 3 on [−6, 3] dB, at the 1.44 MHz band (the code's) and at 100 MHz. Verify: `pytest tests/examples/test_ltbf_cg.py -k float`; the step 2.2 script.
- [ ] **AC3 (A) Bit-exact fixed point.** The fixed-point LTBF CG is bit-exact to an `ap_fixed` C-simulation of the same algorithm on ≥ 20 random problems per regime and `Q̂` form at N_rx ≤ 64, including a zero-divisor case; at a 24-bit format (the widest whose accumulators fit `matmul.acc_format`'s 64-bit limit at K = 1024) its SINR equals the float pipeline's within 0.01 dB. Verify: `pytest -m vitis -rs tests/examples/test_ltbf_cg_conformance.py`; `pytest tests/examples/test_ltbf_cg.py -k fixed`.
- [ ] **AC4 (A) Accuracy DSE.** The §9 (b) grid evaluated with no Vitis in the loop, one command, deterministic; `paper_data/accuracy_losses.csv` holds median and 10th-percentile SINR loss per (band, scenario, array, t, format, `Q̂` form, regime, r); re-running a pre-chosen subset (2 drops over the whole grid) reproduces its rows byte for byte, and the figures are byte-stable. Verify: the step 3.2 subset command and `cmp`.
- [ ] **AC5 (A) Factored engine.** The factored engine is bit-exact at RTL (XSI) against the fixed-point model at N_rx = 64 (per-UE and joint), and csynth reports an estimated clock ≤ 4 ns at N_rx 64 and 256. Verify: `pytest -m xsi -rs tests/examples/test_ltbf_cg_xsi.py -k factored`; the csynth reports.
- [ ] **AC5b (B) Explicit engine and 1024.** The explicit engine is bit-exact in pysim at N_rx = 64 and at RTL (XSI) at 64, with csynth ≤ 4 ns at 64 and the largest size that fits (recorded); the factored engine's csynth at N_rx = 1024 (or its model from 64/256, flagged). Verify: `pytest -m xsi -rs tests/examples/test_ltbf_cg_xsi.py -k explicit`; the reports.
- [ ] **AC6 (A) A53 eigendecomposition.** The 8×8 Hermitian Jacobi eigendecomposition and `Gᵢ` (r ∈ {1, 2}) run as a `SwFunction` on the A53 model, calibrated on gem5 v25.1 HPI with a pre-registered campaign; cycle-model error on held-out inputs ≤ 10% (median). Verify: `pytest -m gem5 -rs tests/examples/test_ltbf_cg_cpu.py`.
- [ ] **AC7 (A) Cost models.** Resource (DSP, LUT, FF, BRAM, URAM) and cycle models of the engines' blocks, from fit builds spanning the target sizes, scored on **≥ 8 held-out** builds fixed before fitting: median relative error ≤ 10% for cycles and DSP, ≤ 15% for LUT, FF, BRAM and URAM, worst case reported. Verify: the step 4.9 command; `paper_data/model_validation.csv`.
- [ ] **AC8 (A) The DSE and its findings.** The joint accuracy × cost space explored in Python; "cost" is each resource's utilization fraction of the xczu48dr and their maximum, and "time" is the long-term update of all three sectors, with budgets of T_LT = 10 ms and 1 ms and 5 ms; the Pareto frontiers per (band, scenario, array, loss budget); the findings of §9 (d) stated in numbers in `FINDINGS.md` and §16, each pointing at its table. Verify: the step 5.2 command; `FINDINGS.md`.
- [ ] **AC9 (B) Decision fidelity.** The brute-force sub-grid, pre-registered (SHA-256 in §15) before any build runs, is measured with Vitis HLS (and RTL where the cycle count needs it). A decision is (scenario, loss budget, time budget, cost measure); the model's pick (the least predicted cost among designs predicted to meet both budgets) is *right* when its measured time ≤ 1.02 × the time budget and its measured cost ≤ 1.10 × the least measured cost among designs measured to meet both budgets. ≥ 90% right per cost measure. Verify: the step 5.3 command.
- [ ] **AC10 (B) Implemented finalists.** 2–4 finalists through Vivado implementation (2024.1) meet 4 ns (WNS ≥ 0); post-implementation resources compared with the model. Verify: `paper_data/finalists_impl.csv`.
- [ ] **AC11 (A) Regression and lint.** The fast suite shows no failures beyond the step 0.4 baseline; new files pass `ruff check` and `black --check`; changed existing files keep their ruff finding counts. Verify: §11.
- [ ] **AC12 (A) Deadline.** No compute job runs past 2026-10-15 18:30 EDT; §16 written by 20:00 EDT. Verify: §15 timestamps.

## 5. Context and sources of truth

| Source | What it is | Access | Freshness / version |
|---|---|---|---|
| LTBF paper | The algorithm (§III-B.1; eqs. (Halpha), (cg_rhs), (CG_K)–(CG_omega)), the regimes, the parameters (`simulation.tex` Table "parameters"), the accuracy method | `/home/wirelesslab914/ali/salsa-ltbf/{solution,simulation,analysis}.tex` (read-only) | HEAD `d2ffa83` |
| The paper's deviation log | D5 (`Iserved`), D12 (rank one), D18/D20 (scattering), D21 (1.44 MHz band) and the rest | `salsa-ltbf/docs/known_deviations.md` (read-only) | same HEAD |
| Paper code | `cg_solve_complex` (`salsa/salsa/utils.py:557`); long-term statistics and projection (`salsa_cap.py:540–700`); SINR (`salsa_cap.py:268–292, 968`); the second snapshot (`salsa_cap.py:920–945`); geometry and arrays (`ray_tracing.py:249, 351–355, 466–547, 589–592, 660–705`); solver settings (`salsa_main.py:150–165`); configs (`salsa/salsa/configs/*.json`) | read-only; semantics only, never imported or used as data | same HEAD |
| Denver scene | Mitsuba XML + 2,573 PLY meshes (about 12 MB; `denver.blend` not needed) | copied once into `examples/ltbf_cg/data/denver/` (step 0.3); only the copy is used | — |
| `waveflow.linalg` | Bit-exact `matmul` (`acc_format` ≤ 64 bits), multi-RHS `cg` model, HLS bodies `waveflow/build/systolic_core_task.h`, `cg_vector_task.h`, cost models `cost.py`, `cg_cost.py` (calibrated at Mmax/Kmax ≤ 16, Nmax ≤ 32; LUT/FF fits UNCALIBRATED; `INDEX_BITS` 16) | `waveflow/linalg/`, `docs/guide/linalg/` | this branch |
| Previous CG DSE study | Sweep runner, measurement harness, models, DSE, fidelity, finalists, implementation check; its lessons (background-task kills, BLAS oversubscription, csynth memory) | `examples/mimo_cg/`, `plans/mimo_cg/mimo_cg_paper_sims.md`, `plans/mimo_cg/mimo_cg_lessons.md` | this branch |
| `waveflow.rfenv` | `SceneSpec`, `DeviceSpec`, `ArraySpec`, `SolverSpec`, `load_or_trace`, `TraceChannelStep`, `taps` | `feat/sionna-prod` (`8ca01221`), merged in step 0.2; `docs/guide/rf/rfenv/index.md` | sionna-rt 2.2.0 |
| `waveflow.cpu` | `Processor`, `SwFunction`, `CpuPlatform`; calibration `waveflow/cpu/calib/` (`calibrate.py` refuses `fit` on the shipped platform, `prereg.py` needs rows in `sweep.py`) | this branch; `docs/guide/cpu/` | gem5 v25.1 |
| Vitis HLS / Vivado | csynth, C-sim, XSI, implementation | `/tools/Xilinx/{Vitis_HLS,Vivado}/2024.1/` | 2024.1 |

## 6. Environment

- **OS / shell:** Ubuntu 24.04 (Linux 6.17), bash. 8 cores, 31 GB RAM (24 GB available), about 108 GB free disk.
- **Languages and runtimes:** Python 3.12.3 (`.venv`); g++ 13.3.
- **Key dependencies:** numpy 2.5.3, scipy; sionna-rt 2.2.0, Mitsuba 3.9.1, Dr.Jit 1.5.0 (`llvm_ad_mono_polarized`, LLVM 20); Vitis HLS / Vivado 2024.1; gem5 v25.1 (`~/ali/tools/gem5/build/ARM/gem5.opt`, image `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1`), McPAT (`~/ali/tools/mcpat`).
- **Commands:** fast suite `pytest -m "not vitis and not xsi and not gem5 and not sionna" -q -rf`; gates `pytest -m vitis|xsi|gem5|sionna -rs`; lint `ruff check <paths>`, `black --check <paths>`.
- **Project structure:** `waveflow/linalg/`, `waveflow/rfenv/` (after 0.2), `waveflow/cpu/`, `examples/mimo_cg/` (previous study), `examples/ltbf_cg/` (this study).
- **Limitations:** no GPU (Sionna on CPU); 2024.1 tools only; the paper's results and environment (sionna-rt 1.2.0, TensorFlow, another host) are unavailable, so its numbers are a qualitative target.

## 7. Existing system and reuse

- **Current implementation.** `waveflow.linalg` has a bit-exact complex fixed-point matrix multiply and multi-RHS CG with HLS bodies, units, RTL gates and cost models calibrated on xczu48dr at 250 MHz, all at detector sizes (k ≤ 16, n ≤ 32) and on chip. `examples/mimo_cg/` composes them into a CG MMSE detector and holds the previous study's DSE tooling.
- **Reuse.** The `matmul`/`cg` arithmetic models unchanged; their HLS bodies as the starting point of example-level bodies; `examples/mimo_cg`'s sweep runner, harness (`hw/measure.py`), fitting (`hw/models.py`), DSE (`hw/dse.py`), fidelity (`hw/fidelity.py`), finalists (`hw/finalists.py`) and implementation check (`hw/impl_check.py`), imported where they fit; `waveflow.rfenv` for tracing; `waveflow/cpu/calib` for the gem5 campaign.
- **Conventions.** `CLAUDE.md`; `plans/source_layout.md` (generated `include/`, `gen/`, `xsi/`, `*_proj/` untracked; authored C++ in `src/`); HLS bodies follow `docs/guide/vectorization/hls/loop_optimization.md`; never pack words by hand.
- **Docs to keep in sync:** none required (the example is not curated).

## 8. Tools and access (readiness)

Checked 2026-10-13 20:29 EDT. Re-run before starting:

```bash
# guided-task plugin's readiness script (run via marketplace_env, see its SKILL.md):
export MARKETPLACE_ROOT=/home/wirelesslab914/ali/claude-marketplace
. "$MARKETPLACE_ROOT/common/scripts/skill_env.sh"
READY="$MARKETPLACE_ROOT/common/plugins/guided-task/skills/guided-task/scripts/check_readiness.py"
"$PY" "$READY" --tool git --tool g++ --tool docker \
  --path /tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls --path /tools/Xilinx/Vivado/2024.1/bin/vivado \
  --path .venv/bin/python \
  --path /home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/data/Denver/denver.xml \
  --path /home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/data/Denver/meshes \
  --path /home/wirelesslab914/ali/tools/gem5/build/ARM/gem5.opt --path /home/wirelesslab914/ali/tools/mcpat \
  --writable plans/mimo_cg --writable examples --git .
# Read-only probes:
source .venv/bin/activate
/tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls -version | grep -m1 v2024
/tools/Xilinx/Vivado/2024.1/bin/vivado -version | grep -m1 "vivado v"
python -c "import sionna.rt, mitsuba as mi, importlib.metadata as m; print(m.version('sionna-rt'), mi.variant())"
docker image inspect ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1 --format '{{.Id}}'
nproc; free -g; df -h /home/wirelesslab914
```

| Check | Target | Status | Detail |
|---|---|---|---|
| tool | `git` | ✅ ready | git version 2.43.0 (/usr/bin/git) |
| tool | `g++` | ✅ ready | g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0 (/usr/bin/g++) |
| tool | `docker` | ✅ ready | Docker version 29.4.1, build 055a478 (/usr/bin/docker) |
| path | `/tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls` | ✅ ready | file, readable |
| path | `/tools/Xilinx/Vivado/2024.1/bin/vivado` | ✅ ready | file, readable |
| path | `.venv/bin/python` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/data/Denver/denver.xml` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/data/Denver/meshes` | ✅ ready | directory, readable |
| path | `/home/wirelesslab914/ali/tools/gem5/build/ARM/gem5.opt` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/tools/mcpat` | ✅ ready | directory, readable |
| writable | `plans/mimo_cg` | ✅ ready | directory, writable |
| writable | `examples` | ✅ ready | directory, writable |
| git | `.` | ✅ ready | branch `feat/cpu-model`, working tree clean |

**Readiness: READY** — 13 ready · 0 to check · 0 blocked

Probes (2026-10-13): Vitis HLS v2024.1; Vivado v2024.1; sionna-rt 2.2.0, Mitsuba 3.9.1, Dr.Jit 1.5.0, `llvm_ad_mono_polarized`; gem5 image `sha256:a06a11d9…` (2025-12-09); Python 3.12.3; 8 cores, 31 GB RAM (24 GB available), 108 GB free.

| Capability | Kind | Purpose | Available? |
|---|---|---|---|
| Explore / general-purpose subagents | agent | code reading; background reviews | yes |
| Monitor, ScheduleWakeup | harness tools | polling detached jobs (Rule 7) | yes |
| `waveflow` MCP server | MCP | optional guide lookups | yes (not required) |
| The user | person | 👁 gates, unexpected conflicts, budget overruns | yes; each wait costs deadline time |

**Gaps the user accepted:** none.

## 9. Approach

**Chosen approach.** Accuracy first in Python, the hardware queue started as early as its
inputs exist, everything long detached and resumable; the DSE joins accuracy and cost through
calibrated models; validation is pre-registered early, queued behind calibration, and cut
first if behind.

**(a) Drops.** `examples/ltbf_cg/scenario.py` reproduces §1's geometry and solver settings as
`rfenv` `SceneSpec`s from a seed: the scene copy, 3 BS sectors, 30 UEs on the coverage grid,
the 32×32 synthetic array, two snapshots T_LT apart. If the antenna pattern or solver settings
are not expressible through `SceneSpec`, stop (Rule 6 d): changes to `waveflow/rfenv` are not
pre-approved. The step 1.2 spike also tests **reciprocity**: tracing the 3 BS sectors as
transmitters (3 sources instead of 30), checked against the UEs-as-transmitters channels on
one drop; used only if they agree to single-precision tolerance. `ltbf_link.py` turns channel
files into per-sector wideband channels over the chosen band (100 MHz: 792 subcarriers at
120 kHz; 1.44 MHz: the code's `scs·n_sc_rb` grid), the sector nuller, power control to the
target range, the SRS draw (8 random distinct subcarriers per UE, shared across sectors,
independent noise per sector, G_SRS or noiseless), `Ĥα`, `Q̂` per sector, and the `Iserved`
rule; sub-arrays for 16×16 and 8×8 by element selection at the same spacing.

**(b) Accuracy grid (no Vitis).** Exact float LTBF (direct solve) is the reference. One
10-iteration run per configuration gives every t ≤ 10 as its prefix, and r = 1 and r = 2 come
from the same `Aᵢ`. Per-UE and joint solves are accuracy-identical (per-column `ω`, `ν` with
static formats): a test asserts the identity, and grouping stays a hardware axis only. Axes:
band (100 MHz, 1.44 MHz), scenario (SINR range, SRS noise), array, format (word width
10–24 with guard bits by the previous study's rule), `Q̂` form (equal in float, different in
fixed point), regime. The SINR is evaluated in low rank (`GᵢQGᵢᴴ = GᵢGᵢᴴ + (GᵢH_c)(GᵢH_c)ᴴ`),
never forming an N_rx × N_rx matrix per subcarrier. Runner: the `SweepRunner` pattern,
detached, ≤ 4 workers.

**(c) Hardware.** Example-level HLS bodies (`examples/ltbf_cg/src/`), derived from the linalg
bodies but streaming the long dimension instead of fully partitioning it: a thin-product
systolic core (k up to 1024, n up to 80 per sector solve) that keeps `Ĥα` resident in a store
usable in both orientations (or reloads it twice per iteration; the cycle model accounts for
whichever is built), and a CG vector body over N_rx × n state. On-chip memory at N_rx = 1024
with 240 columns: `Ĥα` about 0.98 MB (16-bit complex); joint CG state (X, R, D, S at
1024 × 80) about 2.6 MB; about 3.6 MB of the xczu48dr's about 7.6 MB of BRAM + URAM before
banking overhead; per-UE grouping needs about 0.3 MB of state. Explicit `Q̂`: `Q̂` formation
`I + ĤαĤαᴴ` and `S = Q̂D`, on chip up to about 256 (256 KB); 1024 modeled. The engine hands
`Aᵢ` (or `Mᵢ`) to the A53, which runs the 8×8 Jacobi eigendecomposition and forms `Gᵢ`.

**(d) Findings the DSE must state** (user): (1) the cheapest design meeting each loss and time
budget per array size; (2) the explicit-vs-factored crossover (with 240 columns, factored is
cheaper only when its two thin products beat one `N_rx × N_rx` product, about when
2·240 < N_rx, so explicit likely wins at 16×16: test it, don't assume it); (3) the
iterations-vs-word-length trade; (4) where the eigendecomposition should run; plus (5) the
DSE's decision fidelity.

**Search before building:** `waveflow.linalg` (arithmetic models, bodies as templates, cost
model structure), `waveflow.rfenv`, `waveflow.cpu` and its calibration harness,
`examples/mimo_cg` tooling. New code is limited to the LTBF pipeline, the example-level
bodies and engines, the eigendecomposition kernel and the glue.

Alternatives considered:

| Option | Pros | Cons | Fits? |
|---|---|---|---|
| Accuracy first, hardware queue started early, detached jobs, validation pre-registered early (chosen) | Tier-A data early; compute overlaps coding; slack for the queue | Integration risk late | ✅ deadline and tiering |
| Hardware first | Earliest hardware numbers | Accuracy late; idle compute | ❌ risks the core claim |
| The paper's own code for channels | Paper-identical | sionna-rt 1.2 + TensorFlow; data from outside the repo | ❌ user's data rule |
| Extend `waveflow/linalg` to large sizes | Reusable | Slower now; framework diff | ❌ user chose example-level |
| DDR-tiled explicit engine at 1024 | Complete comparison | High build risk in 48 h | ❌ user chose model-only |

## 10. Steps

**Time boxes** (wall clock from T0 = the user's *Start*; absolute stops win):

| Phase | Box | Hard stop |
|---|---|---|
| 0 Setup | 2 h | — |
| 1 Drops (tracing detached) | coding 3 h; tracing ≤ 8 h | tracing done by 2026-10-14 14:00 EDT |
| 2 Float + fixed-point pipeline | 7 h | — |
| 3 Accuracy DSE (sweep detached) | sweep ≤ 8 h | sweep done by 2026-10-14 23:00 EDT |
| 4 Hardware + A53 (queue detached) | coding 12 h; queue ≤ 14 h | queue done by 2026-10-15 15:00 EDT |
| 5 DSE, fidelity, finalists | 5 h; finalists ≤ 4 runs | all compute by 2026-10-15 18:30 EDT |
| 6 Close-out | 1.5 h | 2026-10-15 20:00 EDT |

### Phase 0 — Setup (milestone M0)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 0.1 | Create `paper/cg_dse` from `feat/cpu-model`; create the lessons file; commit this plan and the lessons file | this plan | On the branch; `git rev-parse feat/cpu-model` = `e0f12e19…` | `git branch --show-current` | commit | ☑ |
| 0.2 | `git merge --no-ff --no-commit feat/sionna-prod`. Pre-approved: resolve the known `tests/conftest.py` conflict by keeping both histories, `WANT_XSI_GATES` = the measured merged count (`pytest -m xsi --collect-only -q`), the gem5 and sionna floors kept; accept the root `.gitignore` lines the merge brings. **Any other conflict: stop and ask.** Then `pytest tests/rfenv -m "not sionna"`, `pytest -m sionna -rs`, `pytest tests/build/test_xsi_session_gate.py`; commit | `feat/sionna-prod` | Merged; those pass with 0 skipped; `feat/sionna-prod` unchanged | the commands | commit (merge) | ☑ |
| 0.3 | Copy `denver.xml` and `meshes/` (not `denver.blend`) into `examples/ltbf_cg/data/denver/`; create `examples/ltbf_cg/.gitignore` (`data/`, `paper_data/`, `results/`, `logs/`, `runs/`, `gen/`, `include/`, `xsi/`, `*_proj/`); record the copy's `rfenv` XML-scene hash in §15 | the scene | `git check-ignore -v examples/ltbf_cg/data/denver/denver.xml` matches; `git status --short examples/ltbf_cg` shows only `.gitignore` | the commands | commit | ☑ |
| 0.4 | Baseline: the fast suite, detached (about 8 min); record its failures as this branch's baseline and the ruff counts of files this plan may touch | — | Recorded in §15 | the fast-suite log | — | ☑ |

### Phase 1 — Drops (milestone M1)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 1.1 | `examples/ltbf_cg/scenario.py` (§9 a, a drop's `SceneSpec`s from a seed) and `examples/ltbf_cg/ltbf_link.py` (channel files → per-sector wideband channels, nuller, power control, SRS, `Ĥα`, `Q̂`, `Iserved`, sub-arrays); tests | the paper text and code | Tests pass: geometry ranges; the solver settings of §1; SRS noise at G_SRS; power control lands connected UEs in the target range; `Q̂ ⪰ I`; a sub-array equals a direct smaller-array trace on one drop | `pytest tests/examples/test_ltbf_cg.py -k "scenario or link"` | commit | ☑ |
| 1.2 | **Spike** on one drop: seconds per snapshot at 32×32 with UEs as transmitters and with the 3 sectors as transmitters (reciprocity check); disk per snapshot; set the drop count D so tracing fits ≤ 8 h on ≤ 4 cores and ≤ 45 GB (aim 100, minimum 20); record in §14 | 1.1 | D, the transmitter choice and the measurements recorded | the spike's output | commit (plan) | ☑ |
| 1.3 ⏱ | Trace the drops **detached** (Rule 7), ≤ 4 workers, `nice`, resumable through `rfenv`'s cache, under `examples/ltbf_cg/data/drops/`; manifest with seeds and hashes; PID in §15 | 1.2 | D drops × 2 snapshots exist; re-tracing one drop reproduces it bit for bit | `pytest tests/examples/test_ltbf_cg.py -k drops` (AC1) | commit (code only) | ☑ |

### Phase 2 — Float and fixed-point LTBF pipeline (milestone M2; overlaps 1.3)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 2.1 | `examples/ltbf_cg/ltbf.py`: float LTBF per sector: exact (direct solve); multi-RHS CG with explicit and factored `S`; regimes 1 and 2; per-UE and joint; symmetrization; `Mᵢ`; EVD; `Gᵢ`; the low-rank SINR of §1; instantaneous MMSE as context | 1.1 | AC2 (a), (b) | `pytest tests/examples/test_ltbf_cg.py -k float` | commit | ☑ |
| 2.2 | AC2 (c): the paper comparison on the first traced drops (regime 1; noiseless and nominal SRS; 1.44 MHz and 100 MHz); numbers to §15 | 1.3 (partial), 2.1 | Recorded | the step's script | commit | ☑ |
| 2.3 | `examples/ltbf_cg/ltbf_fixed.py`: bit-exact fixed-point LTBF CG on `waveflow.linalg`'s `matmul`/`cg` models, a formats dataclass, the zero guard; `src/ltbf_ref.h` + a C-sim conformance harness on `examples/mimo_cg/cpp`'s pattern; the per-UE/joint identity test | 2.1 | AC3; the identity test passes | `pytest -m vitis -rs tests/examples/test_ltbf_cg_conformance.py`; `pytest tests/examples/test_ltbf_cg.py -k fixed` | commit | ☑ |
| 2.4 | Background reviewer agent on M0–M2 | — | Findings queued | — | — | ☑ |

### Phase 3 — Accuracy DSE (milestone M3; sweep detached)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 3.1 | `examples/ltbf_cg/ltbf_accuracy_sweep.py` (§9 b), resumable, deterministic; a 1% pilot sets the run time; trim the grid to fit ≤ 8 h and the hard stop (trim recorded in §14) | 2.3, 1.3 | Pilot time and grid recorded | the pilot | commit | ☑ |
| 3.2 ⏱ | Run the sweep **detached** (Rule 7), ≤ 4 workers; merge into `paper_data/accuracy_losses.csv`; re-run the pre-chosen subset and `cmp` | 3.1 | AC4 | the subset re-run + `cmp` | commit (code only) | ☐ |
| 3.3 | Accuracy figures (loss vs t × width per array and band; explicit vs factored in fixed point; regime); candidate findings (3) in §14 | 3.2 | Figures exist; candidates written | `python -m examples.ltbf_cg.ltbf_accuracy_figures` | commit | ☑ |

### Phase 4 — Hardware and A53 (milestone M4; starts when 2.3 passes; overlaps Phase 3)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 4.1 | Example-level HLS bodies (§9 c) and their bit-exact C-sim against `ltbf_fixed` at small sizes; block-level csynth at N_rx 64 and 256 as soon as they compile | 2.3 | C-sim bit-exact; block csynth reports | `pytest -m vitis -rs tests/examples/test_ltbf_cg_blocks.py` | commit | ☑ |
| 4.2 | The factored engine (`hw/engine.py`), pysim bit-exact against `ltbf_fixed` at 64 and 256 (per-UE and joint) | 4.1 | pysim tests pass | `pytest tests/examples/test_ltbf_cg_hw.py -k factored` | commit | ☑ |
| 4.3 | Factored engine csynth + XSI at 64; csynth at 256 | 4.2 | AC5 | `pytest -m xsi -rs tests/examples/test_ltbf_cg_xsi.py -k factored` | commit | ☑ |
| 4.4 | Knob space (`hw/space.py`): R, C, L, units, grouping, formats, sizes (fit builds spanning 64–1024 where buildable; widened index bits where `INDEX_BITS` 16 overflows) | 4.1 | Space defined | unit test | commit | ☑ |
| 4.5 | **Pre-register** the fit/held-out split (≥ 8 held-out) **and** the brute-force sub-grid with its decision rule (§4 AC9), sized from the block-build rate to fit the queue box; files local, SHA-256 committed in §15 | 4.4 | Hashes committed | — | commit | ☑ |
| 4.6 ⏱ | The build queue **detached** (Rule 7): fit → held-out → brute force, ≤ 4 workers (≤ 2 at N_rx ≥ 256, 10 GB watchdog), resumable, reports harvested and build trees removed as they finish | 4.5 | Builds merged into local tables, or the cut recorded at the box | the queue's summary | commit (code only) | ☑ |
| 4.7 | The explicit engine (tier B): `Q̂` formation + `S = Q̂D`; pysim at 64; csynth + XSI at 64; csynth at the largest size that fits; factored csynth at 1024 | 4.3 | AC5b, or the cut recorded | `pytest -m xsi -rs tests/examples/test_ltbf_cg_xsi.py -k explicit` | commit | ☑ |
| 4.8 | A53: the 8×8 complex Hermitian Jacobi eigendecomposition and `Gᵢ` as a C kernel with its Python twin, the `Kernel` entry and the `Family`; a new local platform directory; pre-registration rows in `waveflow/cpu/calib/sweep.py` (pre-approved, additions only); the gem5 campaign (≤ 1 h compute) | 2.1 | AC6 | `pytest -m gem5 -rs tests/examples/test_ltbf_cg_cpu.py` | commit | ☑ |
| 4.9 | Fit the block models (`hw/models.py`); held-out validation table and figure; the explicit-at-1024 DDR model stated | 4.6 (fit + held-out) | AC7 | `python -m examples.ltbf_cg.hw.validate` | commit | ☑ |
| 4.10 | Background reviewer agent on M3–M4 | — | Findings queued | — | — | ☑ |

### Phase 5 — DSE, fidelity and finalists (milestone M5)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 5.1 | Long-term-update time: three sector solves, engine cycles (including `Ĥα` reloads if built that way), the A53 time, transfers; the DDR model for explicit at 1024 | 4.8, 4.9 | Per-design times; unit test | `pytest tests/examples/test_ltbf_cg_hw.py -k time` | commit | ☐ |
| 5.2 | The DSE (`hw/dse.py`): join accuracy × cost; frontiers; the findings of §9 (d) in `FINDINGS.md` | 3.2, 5.1 | AC8 | `python -m examples.ltbf_cg.hw.dse` | commit | ☐ |
| 5.3 | Score decision fidelity on the brute-force builds the queue measured | 4.6, 5.2 | AC9, or the shortfall with its cause | `python -m examples.ltbf_cg.hw.fidelity` | commit | ☑ |
| 5.4 ⏱ | Implement 2–4 finalists (Vivado 2024.1), each started only if it ends before 18:30 EDT | 5.2 | AC10, or the cut recorded | `paper_data/finalists_impl.csv` | commit | ☐ |

### Phase 6 — Close-out

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 6.1 | Regression (§11); figure index; `FINDINGS.md` | all | AC11 | §11 commands | commit | ☐ |
| 6.2 | Final reviewer agent if time allows (non-blocking); §16; status | all | §16 by 20:00 EDT; AC12 | — | commit | ☐ |

Milestones: M0 = 0.1–0.4 · M1 = 1.1–1.3 · M2 = 2.1–2.4 · M3 = 3.1–3.3 · M4 = 4.1–4.10 · M5 = 5.1–5.4 · close-out 6.1–6.2. Milestones do not pause (Rule 6).

Flags: ⚠️ irreversible (none planned) · 👁 gate (none left open; §14 records the decided ones) · ⏱ a budgeted, detached compute job (Rules 5, 7).

## 11. Verification strategy

- **Per step:** the step's *Verify with*. The float pipeline against direct solves; the fixed point against `ap_fixed` C-simulation; the engines against the fixed-point model in pysim and at RTL; the models against held-out builds; the DSE's decisions against brute force.
- **Regression check:** `pytest -m "not vitis and not xsi and not gem5 and not sionna" -q -rf` (no failures beyond the step 0.4 baseline); `pytest -m sionna -rs`; the example's own `-m vitis`, `-m xsi`, `-m gem5` tests; `ruff check examples/ltbf_cg tests/examples/test_ltbf_cg*.py` and `black --check` on the same; the ruff counts of `waveflow/cpu/calib/sweep.py` and any other changed existing file.
- **Independent review:** a read-only `general-purpose` reviewer agent at M2, M4 and the end, in the background (Rule 6); findings and dispositions in §14.

## 12. Safety

- **Version control:** branch `paper/cg_dse`; commit per step; nothing pushed.
- **Diff review by the user:** only on a stop under Rule 6.
- **Irreversible or costly actions:**

  | Action | Step | Backup / recovery | Dry run | Approval |
  |---|---|---|---|---|
  | Merge `feat/sionna-prod` | 0.2 | `git merge --abort`; the branch is new | conflicts listed first | pre-approved for the known conflict; any other: at the time |
  | A compute job beyond its budget in §2 | any ⏱ | stop the job; finished items stay usable (resumable) | pilot or spike first | at the time |
  | Removing harvested build trees | 4.6 | the reports are copied first; trees are regenerable | — | covered by this plan (local build output only) |

- **Permissions:** read-only outside the repo (the paper repo, the AMD tools, gem5); writes inside the repo; Docker for gem5 only.
- **Secrets:** none.
- **Deviation policy:** Rule 4 (2 hypotheses, then stop) and Rule 5 (time boxes; cut and record).

## 13. Agents and models

| Role | Agent / model | Responsibility |
|---|---|---|
| Doer | this session (Claude Opus 5.5) | every step |
| Reviewer | `general-purpose` subagent, read-only | M2, M4 and final reviews, in the background |
| Explorer | `Explore` subagent | optional, for reading `examples/mimo_cg` tooling quickly |

## 14. Decisions, assumptions, risks, open questions

| Kind | Item | Resolution / owner |
|---|---|---|
| Decision | Level L3; the whole long-term update; RFSoC FPGA + A53; Denver re-traced through `rfenv`; the accuracy–cost claim; `Q̂` explicit and factored as an axis; fixed point + float reference; validation reduced for the deadline (held-out + small brute force + 2–4 implementations); explicit `Q̂` measured on chip, modeled at 1024 | user, 2026-10-13 |
| Decision | Branch `paper/cg_dse` from `feat/cpu-model`, `feat/sionna-prod` merged in | user, 2026-10-13 |
| Decision | Scene copied into the repo and kept local; no DSE data, drops or results committed; no data from outside the repo | user, 2026-10-13 |
| Decision | Deadline 2026-10-15 20:25 EDT; scope *full, best effort*; pauses only when required | user, 2026-10-13 |
| Decision | Accepted as suggested: findings (§9 d), the metric and budgets 0.1/0.25/0.5 dB, the scenarios, the design axes with Jacobi preconditioning, warm start and a PL eigendecomposition as nice-to-haves | user, 2026-10-13 |
| Decision | **Plan review** (independent reviewer agent, 2026-10-13: approve with changes; 5 blocking, 13 should-fix, 6 nits). The user decided: the **100 MHz** band with a **1.44 MHz** sensitivity run; the code's **3 sectors × 10 UEs** (`Ĥα` up to 240 columns); **no scattering scenario** (a limitation); **gate 4.0: example-level bodies**, `waveflow/linalg` untouched; drops traced at **32×32 once with 16×16 and 8×8 as sub-arrays**; every other finding applied as written (24-bit AC3 check; the reordered queue with brute force pre-registered in 4.5; detached resumable jobs; thread and memory caps; the reduced grid; the subset re-run for AC4; AC2's relative residual; the memory figures; the cost-model range; defined objectives; ⚠️ for missed targets; the gem5 platform directory; the disk budget; the reciprocity spike; the `.gitignore` exception; regime 1 for the paper comparison; the nits) | user, 2026-10-13 |
| Decision | **Pre-approved** (user, 2026-10-13): resolving the step 0.2 merge's known `tests/conftest.py` conflict as described; the root `.gitignore` lines that merge brings; additions to `waveflow/cpu/calib` (pre-registration rows) and a new local A53 platform directory. **Not pre-approved:** any change to `waveflow/rfenv` (stop if `SceneSpec` cannot express the paper's pattern or solver settings) | user, 2026-10-13 |
| Assumption | The paper's results can be matched qualitatively, not bit for bit (its environment and channels are unavailable; the geometry is reproduced from its text and code) | accepted with the channel choice |
| Assumption | The thin-product and CG-vector bodies stream N_rx = 1024 within the xczu48dr's memory (§9 c) | checked in 4.1 at 64/256 before 1024 |
| Risk | **The deadline.** Mitigation: the order of §10, time boxes, detached queues, tier B cut first, the 18:30 EDT compute stop | doer |
| Risk | Tracing Denver on CPU is slower than estimated, or the disk fills | the spike sets D (≥ 20); the reciprocity option; the 45 GB cap |
| Risk | `SceneSpec` cannot express the tr38901 pattern or the solver settings | a 👁 stop (Rule 6 d) |
| Risk | Large designs exceed timing or memory in csynth | block builds at 64/256 first; 1024 is tier B and modeled if it fails |
| Risk | Waiting on the user stalls work | Rule 6: batch questions; continue independent steps |
| Risk | CPU or memory oversubscription | Rule 8 |
| Limitation | No scattering scenario: Denver specular channels are low-rank (D12); the 100 MHz band mitigates it but does not remove it | user, 2026-10-13 |
| Decision | **One-element trace + the synthetic array applied afterwards** (step 1.1, doer, within the plan's scope): each sector is traced with one tr38901 element and every path's arrival direction is kept; `ltbf_link.channel` applies the n×n array exactly as Sionna's synthetic array does (`exp(j2π/λ·r_ant·k_rx)`, element positions from Sionna's own `AntennaArray.rotate`). Checked against Sionna's own 4×4 synthetic trace on one drop: relative error 1.05e-7 (`test_synthetic_expansion_matches_sionna`). This replaces "16×16 and 8×8 as sub-arrays of the 32×32 trace": every array size comes from the same paths, each centred at the BS as in the paper, and a drop is about 11 KB instead of about 150 MB per snapshot (the 45 GB disk budget is moot) | doer, 2026-10-13 |
| Decision | **Paths sorted per link by delay** before saving: Sionna returned the same paths of one link in a different order on a retrace (step 1.3's test caught it); sorting makes a retrace byte-identical | doer, 2026-10-13 |
| Decision | **Drops: D = 100** (the paper's count): tracing costs about 2.2 s per drop (both snapshots, 30 UEs, 3 sectors, 4 Dr.Jit threads), so all 100 trace in about 4 min; the reciprocity option is not needed | doer, 2026-10-13 (step 1.2) |
| Decision | **The noise floor stays the code's** (`used_bw` = 192 × 120 kHz) in both bands, so the 100 MHz band changes only the channel's frequency diversity, not who is connected | doer, 2026-10-13 |
| Decision | **The loss metric, made precise:** per served UE, `loss = SINR_exact − SINR_approx` in dB (signed: an approximation can beat exact LTBF on noisy SRS estimates, observed on drop 1 at t = 1); a configuration meets a budget when the **median** and the **90th percentile** of the per-UE loss over UEs and drops are both within it ("10th-percentile loss" in §2/AC4 means this upper tail) | doer, 2026-10-13 |
| Decision | `WANT_SIONNA_GATES` 22 → 25 (`tests/conftest.py`), the exact count of the three new `sionna` tests of `tests/examples/test_ltbf_cg.py` (an exact bump, pre-approved by the standing rule for Sionna gate floors); without it `test_sionna_gates` and `test_no_sionna` fail | doer, 2026-10-13 |
| Assumption | The code's sector nuller `P_v` is the identity in the paper's runs (it is set only when interference nulling is on, which is off by default), so it is not modeled | doer, 2026-10-13 |
| Deviation | **The 100 MHz band is sampled at 66 frequencies**, equally spaced edge to edge across the 792 subcarriers (1.46 MHz apart; §9 a said 792): the SRS draw picks 8 of the 66 and the SINR is the mean over them. Measured on drops 1–5, [−6, 3] dB, at 8×8 and 16×16 against all 792 subcarriers: exact-LTBF, MMSE and CG-loss medians and 90th percentiles within 0.02 dB with noiseless SRS, medians within 0.13 dB with nominal SRS (the random SRS subcarriers differ); 6–7× faster | doer, 2026-10-13 |
| Decision | **Fixed-point formats** (step 2.3): every register `ap_fixed<W, I, AP_RND, AP_SAT>`; `I` from a float range profile (largest real or imaginary magnitude) plus the guard bits; vectors (`H`, `Q`, `T`, `B`, `P`, `R`, `S`, `X`) `W` wide (widened to keep 2 fraction bits), the column scalars `min(W + 8, 28)`, `g_div` 6 (the exact `rz'/rz` quotient stays within 64 bits). `Q` is formed as the conjugate of `(Hᵀ)ᴴHᵀ` so the stored `H` is never negated | doer, 2026-10-13 |
| Note | AC3's parenthetical ("the widest whose accumulators fit … at K = 1024") is not exact: with the scalar cap at 28 bits, W = 26 also fits (W = 28 needs a 67-bit intermediate). AC3's criterion (W = 24, ≤ 0.01 dB) is unchanged | doer, 2026-10-13 |
| Decision | **M0–M2 review (step 2.4)**, acted on: (B1) **r = 2 only with SRS noise**: with a noiseless estimate `Mᵢ` is rank one here (≤ 3 paths per link), so its second eigenvector is a round-off direction (85% of UEs, λ₂/λ₁ ≈ 1e-16) and an r = 2 SINR is arbitrary; the sweep evaluates r = 1 only for noiseless SRS. (S1) **Formats per (band, target, SRS, array, N_UE, regime)**: a regime is its own hardware and a design is built for its scenario; profiled on drops 1–10 (pooled groups 1–5 for N_UE > 10), every other drop held out. (S2) **Integer bits may be negative** (no floor at 1): power control shrinks channel entries as 1/√N_rx, so the floor wasted 3–4 bits at 32×32; C-sim conformance re-run with `I` ≤ 0 registers and heavy saturation at both rails: 9/9 bit-exact. (S4) the stress set now saturates H and X at both rails (≈1,700 / 12,000 values) and α, β at the top; the test asserts it. (S5) the long-term link test now checks the blocks against the channel at 8 distinct, sector-shared samples (it was circular). (S6) the three Denver tests trace the local, uncommitted scene copy, so other checkouts would skip them and fail `-m sionna`: they are skip-if-missing tests outside the marker; `WANT_SIONNA_GATES` 25 → 22 (measured). (N1) the float CG now has the paper's per-column stop (`pᴴQp = 0` before the update, `rᴴr < 1e-10` after); the fixed-point unit stops at its own resolution (`rz' = 0` zeroes the next α); it continues rather than stops after a `ps = 0` guard (rare). (N2/N3) docstrings corrected. Not acted on: N4 (drops traced once with the final code; the formats cache now checks its definition), N5, N6 (the reviewer checked real 32×32 ranges: W 16–26 fit) | doer, 2026-10-13 |
| Decision | **The signed loss stays** (review S3 asked to decide before Phase 3): with nominal SRS an early-stopped CG beats exact LTBF (t = 1 by 0.6–1.9 dB median), so a budget on the signed loss can pick t = 1. That is deliberate: the DSE scores the SINR a design achieves, and early stopping regularizes a noisy covariance estimate (as the paper's shrinkage discussion would predict). The findings must state this effect explicitly, and every table carries the absolute median SINR (`median_sinr_db`) beside the loss | doer, 2026-10-13 |
| Decision | **A degenerate fixed-point projection** (`Aᵢ = 0`, or rank < r) gives `GQGᴴ` singular: the SINR uses a pseudo-inverse there and is floored at −60 dB (the UE receives nothing) | doer, 2026-10-13 |
| Deviation | **The sweep runner** is a process pool over per-drop jobs with `SweepRunner`'s concerns (resume by output file, per-job failure isolation, a JSON-lines log, dry run), not `SweepRunner` itself, which is serial and drives build DAGs. Integer products run on float64 BLAS, split so every partial sum stays below 2^53 (bit-identical to `matmul_exact`, tested at the extreme values); 4 workers × 2 BLAS threads | doer, 2026-10-13 |
| Decision | **The accuracy grid (step 3.1)**: 100 MHz and 1.44 MHz × targets [−6, 3], [−6, 14] × SRS noiseless, 10 dB × arrays 8, 16, 32 × drops 1–100; per job the exact LTBF, MMSE, float CG (regimes 1, 2) and 11 formats (W 10, 12, 14, 16, 18, 20, 24 at guard 1; guard 0 and 2 at W 12 and 16; vectors W bits, scalars min(W + 8, 28)) × both `Q̂` forms × both regimes, every t ≤ 10, r = 1 (and 2 with SRS noise); regime 2 at 32×32 only on drops 1–10 at W 12, 16, 24 (B = I has 1024 columns, ~10× the cost). The N_UE sweep: 16×16, 100 MHz, [−6, 3] dB, nominal SRS, N_UE 4, 20, 40 per sector (10 is the main grid), 25 groups each, UEs pooled across drops (the BS and sectors are the same in every drop). 2,475 jobs. Pilot (27 jobs, single-threaded): 8×8 ≈ 10 s, 16×16 ≈ 58 s, 32×32 with regime 2 ≈ 350 s; at 2 threads 16×16 17 s, 32×32 34 s: estimated 4–5.5 h on 4 workers, inside the 8 h box: no trim | doer, 2026-10-13 |
| Decision | **The engine (steps 4.1–4.3)**: one `FreeRunMod` leaf, `hw/cpp/ltbf_engine_task.h`, one sector (or one UE with `Ĥα` kept on chip) per firing; R × C output tiles that stream the long dimension from hand-banked BRAM (`Ĥα` R × R banks, state C column banks, explicit `Q̂` R × C banks, explicit needs R = C); the CG vector step on the tile's C column lanes; `ltbf_ref.h`'s arithmetic unchanged. One complex value per 64-bit beat in and out (`wf_lanes`, L = 1): **L > 1 and `units` (engines side by side) are modeled, not built**. Regime 2 runs its N_rx columns as blocks of `mmax` (B = I's columns are independent), so one engine serves both regimes. RTL gates via the system flow (state_toy's pattern), not the BFM detector harness | doer, 2026-10-13 |
| Decision | **Build sets (step 4.5, pre-registered before any of them was built)**: fit 56 (one knob at a time around a factored and an explicit centre at 256 × 240 × 80, 4 × 4, W16, plus corners at 64 and 1024), held-out 14 (seeded, 4 factored per array + 1 explicit at 64 and at 256, none fitted), brute force 300 (per array: both forms with explicit ≤ 256, square tiles 1–16 + 2 × 8 and 8 × 2, W 10/12/16/20/24, per-UE m = 8 and joint m = 80; cmax 240), and AC9's decision rule (`hw/space.py` `DECISION_RULE`: arrays × 2 nominal-SRS scenarios × loss 0.1/0.5/1 dB × time 1/5/10 ms × cost DSP/LUT/FF/BRAM/max). Files local under `hw/prereg/`; hashes in §15. A build's "measured" cycles are the structural count over its csynth loop table (II, depth, trip counts), anchored by XSI cycle counts | doer, 2026-10-13 |
| Note | **A53 finding (step 4.8, for §9 d (4))**: the calibrated A53 prices one UE's `Mᵢ`, Jacobi and `Gᵢ` at 12,519 + 1,405 n + 184 n r + 914 rotations + 1,287 sweeps cycles (gem5 v25.1 HPI, 1.2 GHz): at N_rx = 1024 about 1.5 ms per UE, so `Mᵢ = ĤᵢᴴAᵢ` (64 n complex products in double precision) dominates and a 10-UE sector exceeds T_LT = 10 ms on one core | doer, 2026-10-13 |
| Decision | **Cycles: csynth's structural count, corrected per tile on RTL.** Every engine loop is II = 1, so a job's cycles are counted from its trip counts and per-tile overheads (within 0.3 % of csynth's `ITER` latency on the first three builds); XSI anchors (both forms at N_rx 64, factored at 256: 12 jobs) show csynth 3.74 cycles pessimistic per R × C product tile, after which every anchored job is within 0.29 %. A build's "measured" cycles (held-out, brute force) are its own loop table's count; the DSE's predicted ones use overheads modeled from the knobs | doer, 2026-10-13 |
| Decision | **AC9's rule names no eigendecomposition placement**: the registered score uses the calibrated one (`a53`: the A53 forms `Mᵢ`, runs Jacobi, forms `Gᵢ`); the modeled `pl` placement (the engine forms `Mᵢ` and `Gᵢ`) is scored and reported beside it, not as the acceptance number | doer, 2026-10-13 |
| Decision | **The update time** (step 5.1): per drop, each sector's `n_conn` / `n_own` from power control at the first snapshot; regime 1 solves `8 n_own` columns, regime 2 `N_rx`, both in jobs of ≤ `mmax`, the first loading `Ĥα`; sectors spread over `units` engines longest-first; the A53's UEs over its 4 cores; engine then A53, no overlap; budgets judged on the 90th percentile over the 100 drops | doer, 2026-10-13 |
| Decision | **M3–M4 review (step 4.10)**, acted on: (B1) regime 2's `Aᵢ = X_sym Ĥᵢ` (n × n × 8 per UE) is costed on the engine's tile; (B2) a sector's columns are padded to the tile on the host (zero columns of H and B are exact), pysim refuses unpadded or oversized jobs, the time model counts padded sizes — the reviewer's repro (C = 16, per-UE jobs of 8) and a ragged H (56 columns, R = 16) are now word-exact on the host build; (B3) the DSP model is per width (W10 packs two multiplies per DSP, W20 takes two, W24 moves some to fabric), BRAM uses a per-bank BRAM18 rule, C = 1 builds fold their divisions into the vector overhead — frozen before any held-out report was read (hash in §15); (S2) Jacobi rotations and sweeps per SRS setting measured on real UEs (noiseless 13.5 / 1.3, nominal 134 / 5.2) replace the constants, and the A53 kernel is stated as scalar, unvectorized C (an optimized-kernel sensitivity `a53x4` is modeled, not calibrated); (S3) L is a modeled DSE axis (1 built, 4 modeled); (S4) every accuracy statistic uses only the drops held out from the formats' profile (11–100; pooled groups 6–25), and regime 2 at 32 × 32 is re-run on drops 11–20 (100 MHz; the 1.44 MHz band then has no regime-2 rows at 32 × 32); AC4's subset is drops 11–12 and pooled groups 6–7; (S5) hardware builds use CANON formats, whose cost equals the per-scenario formats' (same widths): the finalists' bit-level behaviour is CANON's, the accuracy scored is the scenario's; (S6) AC9 is reported with and without the 38 brute builds that are also fit builds; its "measured" time uses the same structural count (only the resources are an independent check); the registered rule's loss budgets (0.1/0.5/1 dB) differ from AC8's (0.1/0.25/0.5): the DSE reports all four. Nits: URAM is never used (its criterion is vacuous); 14 multiplies per lane measured (12 in the source: Vitis adds two) | doer, 2026-10-13 |
| Decision | **Rule 8, restored**: the sweep had run 4 workers × 2 BLAS threads beside the 4-worker csynth queue; a job's output is byte-identical at 1 and 2 threads (checked on a 16 × 16 job), so the sweep was restarted at 4 workers × 1 thread (23:41), to go to 8 × 1 once the queue ends; the AC4 subset is unaffected | doer, 2026-10-13 |
| Open question | None at planning | — |

## 15. Progress log

| Date | Step | What happened | Evidence | Deviation |
|---|---|---|---|---|
| 2026-10-13 | plan | Plan written through the guided-task interview (L3); readiness READY 13/13 | §8 | — |
| 2026-10-13 | plan review | Independent reviewer agent: approve with changes (5 blocking, 13 should-fix, 6 nits); the user decided the open items and had the rest applied; plan revised | §14 *Plan review* | — |
| 2026-10-13 21:00 | start | The user approved the revised plan and said start; readiness re-run READY (12 ready, 1 to check: the uncommitted plan itself); T0 = 21:00 EDT | §8 command | — |
| 2026-10-13 21:02 | 0.1 | `paper/cg_dse` created from `feat/cpu-model` (`e0f12e19`); lessons file created; plan and lessons committed | `git branch --show-current` | — |
| 2026-10-13 21:01 | 0.2 | `feat/sionna-prod` merged (`46bf8ee7`); the known `tests/conftest.py` conflict resolved as pre-approved: both histories, `WANT_XSI_GATES` 213 (measured: 212 + the sounder's gate), gem5 12, sionna 22, the three-marker table; the root `.gitignore` lines it brought (pre-approved). `tests/rfenv -m "not sionna"` 150 passed; `-m sionna` 22 passed, 0 skipped; gate-count + layout tests 22 passed; `feat/sionna-prod` = `8ca01221`, `feat/cpu-model` = `e0f12e19`, unchanged | the commands | — |
| 2026-10-13 21:02 | 0.3 | Denver scene copied (`denver.xml` + 2,573 meshes, 12 MB, not the .blend) into `examples/ltbf_cg/data/denver/`; `rfenv` scene hash `b6413b5cf6a5b7e62d1bc3dacff51cd020bd9d38d1055ff423baa2ed8df33c55`; `examples/ltbf_cg/.gitignore` ignores data/, paper_data/, results/, logs/, runs/, gen/, include/, xsi/, *_proj/; only the `.gitignore` is untracked | `git check-ignore -v`; `git status -uall` | — |
| 2026-10-13 21:02 | 0.4 | Baseline fast suite started detached, PID 3828856, log `examples/ltbf_cg/logs/baseline_fast.log` | — | — |
| 2026-10-13 21:10 | 0.4 | Baseline fast suite (started 21:02, `logs/baseline_fast.log`): 8 failed; 6 are the known baseline (`test_cpu_replay_figures…`, the two `test_markov_figures`, `test_vitis_fft::test_generate_writes_the_build_tree`, `test_retrieval_eval::test_generated_code…`, `test_xsi_workspace_copies[vitis_fft]` — a stale untracked workspace); 2 (`test_sionna_gates`, `test_no_sionna`) were caused mid-run by the new `sionna` tests and fixed by the floor bump (§14) | the log | — |
| 2026-10-13 21:12 | 1.1 | `examples/ltbf_cg/scenario.py` (coverage grid, drop generator, `SceneSpec`s with the paper's sectors, patterns, UE placement and solver settings; one-element trace keeping each path's arrival vector; paths sorted per link; CLI) and `ltbf_link.py` (bands, the synthetic-array expansion, power control, SRS, long-term inputs); `tests/examples/test_ltbf_cg.py`: 3 link tests + 3 `sionna` tests. Grid 365 × 277 at 10 m (3% buildings), 1.0 s; synthetic expansion vs Sionna's 4×4: 1.05e-7 | `pytest tests/examples/test_ltbf_cg.py` (13 passed with 2.1's tests and the gate test) | one-element trace (§14) |
| 2026-10-13 21:12 | 1.2 | Spike: 2.6 s per snapshot (30 UEs, 3 sectors); at most 3 paths per link on the first drop (as the paper's D12 says); D = 100 | the prototype run | — |
| 2026-10-13 21:13 | 1.3 | Drops 1–100 traced detached (first pass 231 s; retraced after the path-sort fix, log `logs/trace_drops.log`); about 11 KB per drop | the log; `ls data/drops` | path sort (§14) |
| 2026-10-13 21:14 | 2.1 | `examples/ltbf_cg/ltbf.py`: float LTBF (exact; multi-RHS CG with explicit/factored `S`, every iterate kept; regimes 1 and 2; the canonical projection; low-rank LTBF SINR; Gram-form instantaneous MMSE); AC2 (a) and (b) tests pass (convergence to the direct solve ≤ 1e-8 with relative residual < 1e-12, both forms and regimes; explicit vs factored `S` ≤ 1e-12); the projection spans the top eigenvectors of `Q⁻¹Q_i` | `pytest tests/examples/test_ltbf_cg.py -m "not sionna"` 9 passed | — |
| 2026-10-13 21:14 | 2.2 (part) | Paper comparison, noiseless SRS, 1.44 MHz, [−6, 3] dB, drops 1–3: median(MMSE − exact LTBF) ≤ 0.001 dB at 8×8, 16×16, 32×32 (paper: within 0.1 dB); median t = 3 CG loss ≤ 0.014 dB (paper: t = 3 suffices). Nominal G_SRS 10 dB, drop 1: exact LTBF median −2.4 / −3.4 dB against MMSE −0.5 / +2.7 dB at 8×8 / 16×16 (the covariance-estimation loss the paper attributes to the estimator) | the prototype runs | — |
| 2026-10-13 21:40 | 2.3 | `ltbf_fixed.py` (formats from range profiles; explicit and factored `S` on `waveflow.linalg`'s `matmul`/`cg`; the zero guard; a per-iteration hook), `src/ltbf_ref.h`, `src/run.tcl`, `ltbf_conformance.py`: 9 case sets (both regimes × both forms at N_rx 16, W16; N_rx 64 W12 factored; N_rx 32 W24 explicit regime 2; two W10 g0 sets; a saturating set), 20 random + 1 zero-divisor case each, 10 iterations: all 2,331,840 words bit-exact in Vitis 2024.1 C-sim (5 min 22 s). W24 vs float on a synthetic 16-antenna link: worst SINR gap 3.1e-3 dB over every t ≤ 10, r, regime and form (W16 4.9e-2, W12 0.50). Per-UE and joint solves bit-identical | `pytest -m vitis -rs tests/examples/test_ltbf_cg_conformance.py` 9 passed; `pytest tests/examples/test_ltbf_cg.py -k fixed` 6 passed | the formats; the AC3 note; 66 frequencies (§14) |
| 2026-10-13 21:45 | 2.2 | Paper comparison (`ltbf_paper_check.py`, `results/paper_check.csv`), drops 1–3 (65 served UEs), regime 1, [−6, 3] dB. Noiseless SRS: MMSE − exact ≤ 0.002 dB at every array and band; median CG loss at t = 3 ≤ 0.012 dB (paper: t = 3 suffices). Nominal G_SRS 10 dB, MMSE − exact at 8×8 / 16×16 / 32×32: 2.33 / 5.99 / 10.30 dB (1.44 MHz), 2.38 / 6.27 / 10.71 dB (100 MHz); the paper's revised array table: 2.25 / 6.30 / 10.58 dB. With nominal SRS the CG loss is not monotone in t: t = 1 beats exact by 0.6–1.9 dB (median), t = 2 is the worst at 16×16 and 32×32 (+1.4, +6.2 dB), t = 5 within 0.05 dB | `python -m examples.ltbf_cg.ltbf_paper_check --drops 1 2 3` | — |
| 2026-10-13 21:55 | 2.4 | M0–M2 reviewer (background): 2 blocking, 6 should-fix, 7 nits; acted on as §14 records. B2 checked: W24 vs float on drops 1–3 (100 MHz, regime 1, r = 1, formats per regime): worst UE ≤ 0.01 dB for t ≤ 7 at every array, target and SRS; up to 0.12 dB at t = 8–10 with nominal SRS (the static formats' precision floor as the residual shrinks); regime 2 at t ≤ 4 differs by up to 20 dB where both pipelines are 10–40 dB below exact. **AC3 ⚠️ partial**: bit-exact (9/9 C-sim, re-run after the negative-integer-bit change, 5 min 9 s); the 0.01 dB criterion holds on the synthetic link at every t and on drops for t ≤ 7 | `pytest -m vitis -rs tests/examples/test_ltbf_cg_conformance.py` 9 passed; the drop check | the review decisions (§14) |
| 2026-10-13 22:11 | 3.1 | `ltbf_accuracy_sweep.py` (jobs, per-scenario/regime formats, the merge to `paper_data/accuracy_losses.csv`); pilot and grid as §14 records; the full sweep started detached (4 workers × 2 threads, `nice`), log `logs/sweep.log`, jobs in `results/sweep/` | the pilot | the grid (§14) |
| 2026-10-13 22:30 | 4.8 | `cpu/ltbf_eig.c` + twin `ltbf_eig.py` (bit-exact to the host build and to gem5), `Kernel` + `Family`, `waveflow/cpu/calib/sweep.py` `ltbf_eig_rows()` (addition), `cpu/sweep_plan.csv` committed (`d2cc61d5`) before measuring; campaign 40 points in 7.4 min (empty region 94 cycles), every gem5 output equal to the twin; fit on 20: validation median 0.3 % (max 0.8 %), **test once: median 0.2 %, max 0.7 %** (AC6 ≤ 10 %). Corpus and model local (`data/a53_ltbf/`). `WANT_GEM5_GATES` 12 → 14 (measured) | `pytest -m gem5 -rs tests/examples/test_ltbf_cg_cpu.py` 2 passed; `-k model` passed | A53 finding (§14) |
| 2026-10-13 22:41 | 4.1–4.3 | The engine (`hw/engine.py`, `hw/cpp/ltbf_engine_task.h`, `hw/build.py`, `hw/host_check.py`, `hw/system_build.py`): pysim = model at 64 and 256 (joint and per-UE with `Ĥα` kept); the body compiled with g++ bit-exact on 5 configurations (both forms, 2 × 2 … 4 × 4, W 12–20); csynth (Vitis 2024.1, xczu48dr, 4 ns): factored 64 × 64 × 16 2 × 2 W16 2.867 ns (60 DSP, 13,965 LUT, 8,683 FF, 28 BRAM18, 68 s), explicit same 2.867 ns (60 DSP, 32 BRAM18), factored 256 × 240 × 80 4 × 4 W16 2.867 ns (184 DSP, 24,897 LUT, 16,385 FF, 448 BRAM18, 79 s); every inner loop II = 1. **XSI (both forms, N_rx 64): all 3,072 X words of four jobs equal the model; RTL = pysim** (2 passed, 152 s). XSI cycles (factored): a joint job with `Ĥα` load 174,715, a per-UE job 85,086. `WANT_XSI_GATES` 213 → 215 (measured). **AC5 met**; AC5b met at 64 (the largest-size and 1024 csynths are in the queue) | `pytest tests/examples/test_ltbf_cg_hw.py`; `pytest -m xsi -rs tests/examples/test_ltbf_cg_xsi.py` | the engine (§14) |
| 2026-10-13 22:50 | 4.4–4.5 | `hw/space.py` (knobs, validity, the sets), `hw/queue.py`. **Pre-registered** (local `hw/prereg/`): rule `99f84b9ed6bee752795225716e2a9c9717d1c6f8a4e2f68204d351fd54a0eb23`; fit (56) `e78a9098591999f207539c2e227b5620b98752c7ee48e193ee41ead2c32b245a`; held-out (14) `e53c0b963b577f79840494a8a623197614c30ebf96d07faae9cd7df8aee82d26`; brute force (300) `72e58867da12f72b960b76c109e70a8d29279c95a073607d0e0278685ded8ecd` | `python -m examples.ltbf_cg.hw.space`; `pytest tests/examples/test_ltbf_cg_hw.py -k sets` | build sets (§14) |
| 2026-10-13 23:10 | 4.9 (part), 5.1–5.4 (code), 3.3 (code) | `hw/models.py` (+ overhead fit for a form with no builds yet), `hw/rtl_anchor.py` (256 anchor: RTL X words = model; δ = 3.74 cycles/tile; 12 jobs within 0.29 %), `hw/validate.py`, `hw/timing.py` (per-drop sector populations computed for both bands, both targets, all arrays), `hw/dse.py` (trial on the partial sweep: 32 × 32, nominal, [−6, 3] dB, 32,400 designs in 24 s), `hw/fidelity.py`, `hw/finalists.py`, `ltbf_accuracy_figures.py` (byte-stable on two renders). Partial sweep at 32 × 32: W24 = float to 3 decimals (both forms, both regimes); W10's loss grows with t (nominal SRS, [−6, 3] dB: −1.0 / 2.8 / 4.2 / 6.0 dB at t = 1 / 3 / 5 / 10) | the trial runs | cycles, fidelity, timing (§14) |
| 2026-10-13 23:30 | 4.9 | **Model structure frozen before any held-out report was read** (M3–M4 review B3): DSP per width (a_W x tile multiplies + b_W x lanes, least squares per width with two or more tile shapes; one-shape widths scaled from W16; W18 takes W16's, its operands fitting one DSP48E2), BRAM linear in a per-bank BRAM18 rule's counts per array kind, LUT/FF linear in structural features, overheads linear in R, C, W (C = 1 builds' divisions folded into a_v). In-sample on 41 fit builds: cycles median 0.6 % (max 1.7 %), DSP 1.0 % (16 %), BRAM 8.8 % (26 %), LUT 5.9 % (39 %), FF 4.7 % (22 %). `hw/models.py` sha256 `9d6930c4dc270a2eda477626894f602410355631431ee378a6e37c597de7c198` | the in-sample check | — |
| 2026-10-13 23:27 | 4.10 | M3–M4 reviewer: 3 blocking, 6 should-fix, nits; acted on as §14 records (`d1d6fa9d`, model freeze `0771f55c`) | `pytest tests/examples/test_ltbf_cg_hw.py -k "not 256 and not hls_body"` passed; ragged-size tests passed | M3–M4 review (§14) |
| 2026-10-13 23:41 | 3.2 | Sweep restarted twice (resumable, the 48 finished jobs kept): with regime 2 at 32 × 32 moved to drops 11–20 at 100 MHz (`9667fec5`), then single-threaded per worker (Rule 8); log `logs/sweep3.log`. One job's output byte-identical at 1 and 2 BLAS threads | `cmp` of the two runs' `.npz` | Rule 8 (§14) |
| 2026-10-13 23:51 | 4.9 | **AC7 met** (held-out 14 builds, scored once, `hw/models.py` at its frozen hash, fit on the 56 fit builds): cycles median 0.68 % (worst 2.49 %), DSP 2.48 % (17.1 %), LUT 6.84 % (16.0 %), FF 5.86 % (14.0 %), BRAM 9.11 % (34.3 %), URAM 0 % (never used: vacuous). `paper_data/model_validation.csv`, `figures/model_validation.png`. The explicit engine at 1024 is modeled with Q in DDR (`timing.sector_cycles`) | `python -m examples.ltbf_cg.hw.validate` | — |
| 2026-10-13 23:53 | 4.7 | **AC5b met**: the explicit engine bit-exact in pysim and at RTL (XSI) at N_rx 64 (step 4.3); csynth ≤ 4 ns at 64 and at the largest size kept on chip, 256 (17 builds, ≤ 2.899 ns, ≤ 1,280 BRAM18 of 2,160); explicit at 1024 modeled with Q in DDR; the factored engine's csynth at 1024 (12 builds so far, ≤ 2.897 ns). All 74 builds harvested so far: ≤ 2.917 ns, none failed | the queue's reports | — |
| 2026-10-14 04:11 | 4.6 | **Build queue done**: 332 builds (56 fit, 14 held-out, 300 brute force, 38 shared), 0 failed, 326.5 min wall (≤ 4 builds, ≤ 2 at N_rx ≥ 256, the watchdog never fired); reports in `hw/reports/` (local), build trees removed | `logs/queue.log` | — |
| 2026-10-14 09:40 | 3.2 | **Sweep done**: all 2,475 jobs (2,427 after the restarts + 48 before), 0 failed, 600.6 min for the last run (4 workers × 1 thread); 9.5 h in all, over the 8 h box (two restarts after the M3–M4 review), inside the 23:00 stop. Merged (drops held out from the formats' profile): `paper_data/accuracy_losses.csv`, 17,082 rows, about 1,970 UE samples per main scenario (90 drops); N_UE 4 / 20 / 40: 136 / 876 / 1,755. The session was down 03:00–10:30 (no work lost: everything detached) | `logs/sweep3.log`; the merge | box overrun (§14 rule 7) |
| 2026-10-14 10:40 | 3.3 | Accuracy figures from the full merge: 12 PNGs (loss vs t per width, explicit − factored, regime 2 − regime 1; both bands, both SRS settings), byte-identical on a second render | `python -m examples.ltbf_cg.ltbf_accuracy_figures`; `cmp` | — |
| 2026-10-14 10:48 | 5.3 | **AC9 ⚠️ (scored once, as registered)**: 300 brute builds × 2 scenarios × runtime choices (22,880 designs), 270 decisions per placement. `a53`: DSP 100 %, FF 100 %, BRAM 100 %, **LUT 75.9 %, max 88.9 %** (bar 90 %); without the 38 fit-also builds identical; `pl` (reported, not the acceptance number): 94.4 / 94.4 / 94.4 / 61.1 / 88.9 %. **Cause**: every wrong decision picks a W10 design at 8 × 8 or 16 × 16 where W12 was measured cheaper in LUTs: Vitis packs W10's tile multiplies two per DSP and the packing logic lands in LUTs (a W10 build measured 0.0636 of the device's LUTs against W12's 0.0553), while the LUT model, linear in W²-scaled multiplier bits, predicts W10 cheaper (0.0564); max-fraction decisions fail where LUT is the binding resource. A W10 packing term in the LUT model is the fix (not applied: post hoc). Also as the M3–M4 review noted, the "measured" time is the same structural count, so only the resources test the model independently | `python -m examples.ltbf_cg.hw.fidelity`; `paper_data/fidelity_*.csv` | — |

## 16. Completion report

*Filled in when the work ends.*

- **Changed:**
- **Tested (commands and results):**
- **Acceptance criteria:** AC0 … AC12 ✅/❌/⚠️ with evidence
- **Decisions made:**
- **Assumptions and not verified:**
- **Remaining risks and issues:**
- **Next steps:**
- **Reusable artifacts saved / tools that would have helped:**

## Checklist coverage

| Group | Addressed (where) | Not applicable (why) |
|---|---|---|
| T: task definition, planning | §1–§4, §9, §10 (time boxes), §14 | — |
| C: context and environment | §5–§7 | — |
| V: verification | §4, §10 *Verify with*, §11 | — |
| D: documentation and continuity | lessons file (0.1), §15, §16, `FINDINGS.md` | `CLAUDE.md` and docs: no change (the example is not curated; `waveflow/linalg` untouched) |
| R: version control and safety | §12, Rules 9–13 | ⚠️ irreversible actions: none planned |
| S: security and permissions | §12 | secrets: none needed |
| A: authoritative sources | §5, Rule 2, Rule 14 | — |
| P: plugins, MCP, tools | §8 capabilities, §9 *Search before building* | MCP servers: the `waveflow` server is optional, not recorded in `.mcp.json` |
| M: multi-agent | §11, §13 | — |
