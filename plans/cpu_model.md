# Plan: a reusable, calibrated general-purpose processor model (`waveflow/cpu/`)

**Status:** Ready (approved by the user 2026-10-08) <!-- Ready only after the user approves this written plan -->
**Complexity:** L3 Complex. A new subpackage plus a calibration flow on two tools not yet installed
(gem5, McPAT), with open design choices and accuracy that only new measurements can prove; nothing is
irreversible. Scores: scope 3 · clarity 3 · novelty 3 · dependencies 3 · verification 3 · risk 2
**Created:** 2026-10-08 · **Updated:** 2026-10-08 (moved to a separate clone with its own venv; based on `main` @ `12350fa4`)
**Plan file:** `plans/cpu_model.md` · **Lessons learned:** `plans/cpu_model_lessons.md` (the doer
creates it at the first lesson, in the style of `plans/mimo_cg/mimo_cg_lessons.md` on branch
`paper/mimo-cg`: `git show origin/paper/mimo-cg:plans/mimo_cg/mimo_cg_lessons.md`)

> **For the agent executing this plan:** read *Rules for the doer* first. The
> plan is self-contained. Paths are repo-relative, commands are exact, and
> secrets are named by environment variable only.

## Rules for the doer

1. Before changing anything, re-run the readiness commands in §8. Stop if
   anything required is ❌ blocked.
2. Do one step at a time (§10). A step is done only when its exit condition is
   verified with the stated method. Show the command and its result as evidence.
   Before changing code, read it and its callers. Check the authoritative docs
   for any version-sensitive API (versions in §6). Before building a tool,
   script or integration the plan didn't foresee, search for an existing one
   and propose it first.
3. After each step, tick it in §10, add a line to the §15 progress log, and
   append anything surprising to the lessons-learned file.
4. When something fails, find the root cause from the error, logs, tests and
   actual state. No random fixes. Deviation policy: **investigate up to 2
   hypotheses, test each, then stop and report what you know.**
5. Pause for the user: **at the end of each milestone (M0–M4) and on any
   surprise**, with a two-line status.
6. Never perform an action listed in §12 as irreversible without its backup in
   place and fresh approval from the user at that moment.
7. Never merge, push, deploy or publish unless the user asks.
8. State assumptions and anything you couldn't verify. Never fill a gap with a
   guess.
9. Finish with §16: check every acceptance criterion, run the regression check,
   and write the completion report.

Rules specific to this plan:

10. **Work only in the clone** `/home/wirelesslab914/ali/waveflow-cpu`, a separate clone of
    `https://github.com/sdrangan/waveflow`, on branch `feat/cpu-model`, created from `main` in step 0.
    Another agent is working in the original checkout `/home/wirelesslab914/ali/waveflow`: never edit,
    stash, commit or switch it, and never install into its venv.
11. **Pre-register before measuring.** The **fit / validation / test** sets (step 10) are committed
    before any gem5 run other than the smoke points. The gem5 runner refuses any point that isn't in
    the committed `sweep_plan.csv`, and it stamps the pre-registration commit into every corpus row.
    - Model-structure changes, including the two deviation hypotheses, may look at the **validation**
      set only.
    - The **test** set is evaluated **once**, at the end of step 11 (and of step 12 for area), and
      that number is the acceptance number.
    - If the model changes after the test evaluation, first register a fresh test set and keep the old
      result in §15.
12. **A measured number names its tools** (the `CLAUDE.md` rule): every corpus row records the gem5
    tag and commit, the compiler, its version and flags, the gem5 core, cache and DRAM configuration,
    and, for McPAT, its commit and technology node.
13. Tools live outside the repo under `~/ali/tools/` and run in Docker. Install nothing system-wide.
14. **Use the clone's own venv** `/home/wirelesslab914/ali/waveflow-cpu/.venv`, created in step 0 with
    `pip install -e ".[dev]"`. It is the only place anything is pip-installed. Its editable install
    points at the clone, so `import waveflow` resolves to the clone from any directory; step 0
    verifies this.
15. Format **only new files** with `black`. The house code is not black-clean, so a repo-wide run
    would rewrite unrelated files.

## 1. Goal

Waveflow gains a **reusable model of a general-purpose processor**, a Cortex-A53-class core first. It
covers the control and scalar software that runs poorly on specialized engines: schedulers,
bookkeeping, irregular algorithms. Software is written as ordinary Python functions that report the
work they did (items scanned, queue depth, …). The model charges that work's **time, energy and
memory footprint** from cost models calibrated against **gem5** (cycles) and **McPAT** (energy,
area). It schedules tasks over N cores with priorities and optional preemption, all in the same
loosely-timed SimPy simulation as waveflow's hardware components.

Why it matters: a later design-space exploration plan will repeat the study in
`/home/wirelesslab914/ali/tracerspecsense`, "far more accurate and structured". In that repo a
processor cost `ops / fclk` with hand-counted `ops` (`GenProcPE`, `specsense/tracerres.py:1304`), and
the scheduler's own cost was a hardcoded table of cycles against task count with no recorded origin
(`specsense/scheduler.py:1319`). The DSE needs every processor cost to be measured, reproducible,
reported with a confidence, and swappable per platform, the way waveflow's hardware timing and
resource models already are.

## 2. Requirements and constraints

| Type | Item |
|---|---|
| Must | Live in `waveflow/cpu/`, importable by any example or project. |
| Must | Be loosely timed: compute in Python at zero simulated time, then charge the predicted time in one `timeout`; split a task wherever it interacts with hardware. One timed event per task segment, not per instruction. |
| Must | Model N cores with a priority ready queue, run-to-completion by default, an optional **preemptive** mode (the preempted task's remaining cycles are re-queued), a context-switch cost, interrupt handlers as high-priority tasks, and a **queueing delay** reported per task. |
| Must | Price each software function with a `LinCalibModel` over the **work counters** the Python function returns, plus cache-regime features derived from the configured cache sizes and the declared working set. Keep cycles internally and energy in **pJ**, never in seconds or joules: the exactness tolerance is absolute, `1e-9`. Before calibration a function runs on a seed and reports `UNCALIBRATED`. |
| Must | Expose four cost axes for the DSE: **timing** (per-task latency, queueing delay, core utilization, switch overhead), **energy** (dynamic per task, plus static power × elapsed time), **area** (per core configuration), **memory footprint** (code bytes, working set, cache regime). |
| Must | Attach a `Confidence` (`EXACT` / `INTERPOLATED` / `EXTRAPOLATED` / `UNCALIBRATED`) to every estimate, computed when reported, not on the simulation's hot path. |
| Must | Calibrate against gem5 v25.1.0.1, HPI core, syscall-emulation (SE) mode, configured as the xczu48dr's A53 (clock, caches, DRAM from step 1). C kernels are built static at `-O2` with Vitis 2024.1's `aarch64-linux-gnu-gcc` 12.2.0; each measured kernel function is `noinline`; numeric kernels use **Q15 fixed point**. Energy and area come from McPAT on the same runs. |
| Must | Store measurements as a **TimingModel-style corpus** (`corpus.csv` + `params.json` under the platform's `cpu/` directory) with provenance columns. Don't change `waveflow/calib/record_store.py`. |
| Must | Leave bus, memory-mapped register and polling traffic charged by `MMIFMaster` / `BusTiming` / `poll_until`. The CPU model never charges it a second time. |
| Must | Measure context-switch and dispatch overhead with SE-mode microbenchmarks. The primary context switch is a hand-written cooperative register save/restore with no syscall. `swapcontext` is measured for information only, because SE mode emulates its `rt_sigprocmask` syscall at near-zero cost. Take interrupt entry latency as a flagged constant from a public source, or leave it `UNCALIBRATED`. |
| Must not | Put an instruction-set simulator or gem5 inside the simulation loop. |
| Must not | Import from `examples/` or from tracerspecsense inside `waveflow/cpu/`. |
| Must not | Modify tracerspecsense, the main checkout's working tree, `waveflow/calib/record_store.py`, or any platform under `waveflow/calib/platforms/` other than the new one this plan creates. |
| Must not | Install anything system-wide or into the original checkout's venv, merge, or push. |
| Nice to have | A cross-configuration report: a second cache configuration predicted *without* re-fitting, compared with gem5 (informational, not a gate). |
| Nice to have | A `source` column that already admits a future `board_pmu` value (real A53 cycle counter) without a schema change. |
| Constraint | Python ≥ 3.10 (venv: 3.12.3), SimPy 4.1.2; Linux; no sudo; Docker 29.4.1 usable without sudo; 8 cores, 31 GB RAM, 121 GB free on `/home`. |
| Constraint | Accuracy on the pre-registered **test** set: **median \|relative error\| ≤ 10 %, max ≤ 25 %**, per kernel family (cycles against gem5; the same bounds for energy and area against McPAT, see §14). |
| Constraint | Speed: **≥ 20,000 executed tasks per wall-clock second** on this machine, for both streaming and burst arrivals (step 7), and the scheduler replay **≥ 1,000× faster than gem5**. |

## 3. Deliverables

| Deliverable | Format | Location | For whom |
|---|---|---|---|
| Processor model package | Python | `waveflow/cpu/` | waveflow users; the DSE plan |
| Calibration kernels, gem5/McPAT harness, gem5 config script | C + Python | `waveflow/cpu/calib/` | whoever calibrates a new platform |
| The A53 calibration platform: corpus, fitted params, pre-registration, accuracy tables | CSV / JSON | `waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/cpu/` (clock confirmed in step 1; step 10 fixes the name) | the DSE plan |
| Micro-scheduler example | Python | `examples/cpu_sched/` | users; the DSE plan as its seed |
| Tests | pytest | `tests/cpu/`, `tests/examples/test_cpu_sched*.py` | CI and reviewers |
| Guide and example docs | Markdown (Jekyll front matter) | `docs/guide/cpu/`, `docs/examples/cpu_sched/` | waveflow users |
| Guidance updates | Markdown | `CLAUDE.md`, `docs/overview/status.md` | future sessions; readers |
| gem5 and McPAT builds | binaries | `~/ali/tools/gem5`, `~/ali/tools/mcpat` (outside the repo) | the doer; future calibrations |
| Lessons learned, completion report | Markdown | `plans/cpu_model_lessons.md`, §16 here | the user |

## 4. Acceptance criteria

The task is done when every box is ticked with the evidence named. Every command runs from the
clone root `/home/wirelesslab914/ali/waveflow-cpu` with the clone's venv activated (§6).
`$PLATFORM` ("the platform") is the new platform directory from step 10, e.g.
`waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1`. `$A53_CLK` is the A53 clock confirmed in step 1: 1.2GHz (speed grade -1).

- [ ] **AC1**: `waveflow/cpu/` exports `Processor`, `CpuConfig`, the task and cost types, and the area
  and footprint estimators, and imports nothing from `examples/` or `specsense`. Verify:
  `python -c "from waveflow.cpu import Processor, CpuConfig"` and
  `pytest tests/cpu/test_no_example_imports.py`. *(Step 3.)*
- [ ] **AC2**: Scheduling semantics are exact. At least 12 hand-computed timelines pass, asserting each
  task's start, end, **queueing delay**, core and switch charge. They cover:
  - one and several cores, priority order, FIFO tie-breaking, utilization;
  - preemptive mode: the remaining-cycles rule of step 5 with its rounding, a preemption during a
    switch, a switch charged on resume, an interrupt as a task.

  Verify: `pytest tests/cpu/test_scheduling.py tests/cpu/test_preemption.py`. *(Steps 4–5.)*
- [ ] **AC3**: No double charge. A task that issues `MMIFMaster` traffic gets bus time from the bus
  model only, and its CPU cycles are the same with and without that traffic. Verify:
  `pytest tests/cpu/test_bus_coexistence.py`. *(Step 7.)*
- [ ] **AC4**: Confidence is honest.
  - A seeded function reports `UNCALIBRATED`.
  - A calibrated function reports `INTERPOLATED` inside its fitted feature range (or `EXACT` when its
    fit has zero residual) and `EXTRAPOLATED` outside.
  - The fitted energy models (pJ) don't report `EXACT`.

  Verify: `pytest tests/cpu/test_confidence.py`. *(Seeded part step 6; calibrated part step 11.)*
- [ ] **AC5**: Each Python twin equals its C kernel bit for bit, in outputs **and** work counters:
  - against the **host-gcc** build, on every registered point (`pytest tests/cpu/test_kernel_twins.py`);
  - against the **gem5-run** binary's output, on every measured point, recorded in the corpus column
    `output_matches_twin` (`pytest tests/cpu/test_platform_accuracy.py -k twins`).

  *(Smoke grid in step 8; all points in step 11.)*
- [ ] **AC6**: Cycle accuracy against gem5 on the **test** set: per kernel family, median |rel err|
  ≤ 10 % and max ≤ 25 %. For the overhead microbenchmarks (constant models), the test runs at other
  repetition counts and data placements meet the same bounds against the constant fitted on the fit
  runs. Verify: `$PLATFORM/cpu/accuracy.csv` and `pytest tests/cpu/test_platform_accuracy.py -k cycles`.
  *(Step 11.)*
- [ ] **AC7**: Energy.
  - Dynamic energy (pJ) against McPAT on the test set meets the same bounds:
    `pytest tests/cpu/test_platform_accuracy.py -k energy`.
  - `Processor.report()`'s total energy equals dynamic + static power × elapsed time for each powered
    core, exactly, on a hand-computed case: `pytest tests/cpu/test_report.py`.

  *(Steps 6 and 11.)*
- [ ] **AC8**: The area and leakage models against McPAT on the test configurations, with the same
  bounds. Verify: `pytest tests/cpu/test_platform_accuracy.py -k area`. *(Step 12.)*
- [ ] **AC9**: Footprint.
  - Each kernel's `code_bytes` in the corpus equals its `noinline` symbol's size in the static binary.
    A gem5-marked test rebuilds and compares with `aarch64-linux-gnu-nm -S`.
  - The cache-regime flag is correct on both sides of the L1D and L2 boundaries.

  Verify: `pytest tests/cpu/test_footprint.py` and `pytest -m gem5 tests/cpu/test_footprint_binary.py`.
  *(Steps 6 and 9.)*
- [ ] **AC10**: Scheduler replay. On the example's recorded trace:
  - the model's total scheduler cycles are within 10 % of gem5's;
  - over the measured operations (each in its own measured region, step 14), median |rel err| ≤ 10 %
    and max ≤ 25 %.

  Verify: `pytest -m gem5 tests/examples/test_cpu_sched_replay.py`. *(Step 14.)*
- [ ] **AC11**: Speed.
  - `python -m waveflow.cpu.bench --pattern stream --tasks 100000` and
    `python -m waveflow.cpu.bench --pattern burst --tasks 20000` each report ≥ 20,000 tasks/s.
  - The replay's wall time is ≥ 1,000× below gem5's.

  All three are recorded in §15 with the machine (8-core, Ubuntu 24.04.4). *(Steps 7 and 14.)*
- [ ] **AC12**: Provenance and pre-registration.
  - Every corpus row carries `source`, the gem5 tag and commit, the compiler and its version and
    flags, the core, cache and DRAM configuration, the McPAT commit and node where relevant, and
    `prereg_commit`, which equals the commit that added `sweep_plan.csv`.
  - No validation or test row is in any fit.

  Verify: `pytest tests/cpu/test_provenance.py tests/cpu/test_preregistration.py`. *(Steps 9–11.)*
- [ ] **AC13**: Docs.
  - `docs/guide/cpu/` (concept, scheduling, cost models and calibration, use in a DSE) and
    `docs/examples/cpu_sched/` exist with front matter, checked by a test that lists the expected pages.
  - Any number the new docs quote is guarded by `tests/docs/test_documented_numbers.py`.
  - `CLAUDE.md` gains the `gem5` marker command and the `waveflow/cpu/` architecture line.

  Verify: `pytest tests/docs tests/cpu/test_docs_present.py`. *(Step 16.)*
- [ ] **AC14**: No regression and clean code.
  - `pytest -m "not vitis and not xsi and not gem5"` has no failures beyond the step-0 baseline.
  - `pytest -m gem5` passes with **0 skipped**.
  - `ruff check waveflow/cpu examples/cpu_sched tests/cpu`.
  - `black --check` on the new files only.
  - `mypy --follow-imports=silent waveflow/cpu` is clean.
  - `git diff --stat main -- waveflow/calib/platforms waveflow/calib/record_store.py` lists only the
    new platform directory.

  Verify: the commands' output, pasted in §15. *(Step 17.)*

## 5. Context and sources of truth

| Source | What it is | Access (path, URL, MCP server, command) | Freshness / version |
|---|---|---|---|
| waveflow repo | The codebase; base for the work | the clone `/home/wirelesslab914/ali/waveflow-cpu`, branch `main` (`12350fa4`, 2026-10-08, or newer at step 0) | live |
| `plans/host_runtime.md` | The owner's plan for host software as `SwThread`s on a `SwHost`, with `yield self.compute(cycles)`; overlaps this plan (§14) | the clone | `258fd1c3`, not started |
| `waveflow/hw/mm_host.py`, `waveflow/hw/irq.py` | The host endpoints and interrupt lines `host_runtime.md` builds on | the clone | live |
| `CLAUDE.md` | Commands, architecture, version rule | repo root | live |
| `docs/guide/timing_model/` | The LT philosophy and timing-model conventions this plan follows | repo | live |
| `waveflow/calib/calib.py` | `CalibModel` (`data_dir`, `corpus_path`, `params_path`, `corpus`), `CalibDataFrame`, `LinCalibModel` | repo | live |
| `waveflow/calib/timing_model.py` | The corpus-directory layout this plan copies | repo | live |
| `waveflow/calib/confidence.py` | `Confidence`, `ConfidenceLevel`, `Estimate`, `FitSummary` (`covers`, `outside`, `EXACT_TOL = 1e-9`) | repo | live |
| `waveflow/calib/platform.py` | `Platform`, `Platform.resolve(platforms_root, name, *, part, clk_freq, res_types)` | repo | live |
| `waveflow/simulation/simobj.py` | `SimObj` lifecycle, `process`, `timeout`, `event` | repo | live |
| `waveflow/hw/memif.py` | `MMIFMaster`, `BusTiming`, `poll_until`, `DirectMMIF` | repo | live |
| `waveflow/toolchain/toolchain.py` | `find_vitis_path`, for locating the bundled cross compiler | repo | live |
| `tests/conftest.py` | The `xsi` session gate ("a skip must not read as a pass") and `WANT_XSI_GATES`, the pattern for the `gem5` gate | repo | live |
| `tests/docs/test_documented_numbers.py` | The house guard for numbers quoted in docs | repo | live |
| `tests/linalg/test_no_example_imports.py` | Template for the no-example-imports test | `git show origin/paper/mimo-cg:tests/linalg/test_no_example_imports.py` | branch `paper/mimo-cg` only |
| `plans/mimo_cg/mimo_cg_lessons.md`, `mimo_cg_paper_sims.md` | House style for lessons and pre-registration (9.4a), and the stratification lesson | `git show origin/paper/mimo-cg:plans/mimo_cg/mimo_cg_lessons.md`, `git show origin/paper/mimo-cg:plans/mimo_cg/mimo_cg_paper_sims.md` | branch `paper/mimo-cg` only |
| `examples/shared_mem/hist.py` (`HistController`), `examples/regmap/simp_fun.py` (`SimpFun`), `docs/guide/interface/axi_mm/modeling.md` (`Cpu`) | Today's untimed hosts; the accelerator the example dispatches to | repo | live |
| tracerspecsense | Prior DSE: `specsense/processor.py`, `resource.py`, `scheduler.py`, `tracerres.py` | `/home/wirelesslab914/ali/tracerspecsense` (read-only) | `1be20f0` |
| gem5 docs | Build, SE mode, m5ops, stats, `-P` parameter overrides | https://www.gem5.org/documentation/ | v25.1 |
| gem5 source | `configs/common/cores/arm/HPI.py`, `configs/example/arm/starter_se.py`, `util/m5` (its README gives the cross-build syntax) | `~/ali/tools/gem5` after step 2; https://github.com/gem5/gem5 | tag `v25.1.0.1` |
| gem5 build image | Official all-dependencies image | `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1` | tag `v25-1` |
| McPAT | Area and power from activity counts; `mcpat.mk`; `ProcessorDescriptionFiles/ARM_A9_2GHz.xml` | https://github.com/HewlettPackard/mcpat → `~/ali/tools/mcpat` | `master` @ `74d4759f3ba2dff8f5a69e07a68efdb46b42fb8c` |
| AMD DS926 | Zynq UltraScale+ RFSoC data sheet: A53 maximum clock per speed grade | https://docs.amd.com/r/en-US/ds926-zynq-ultrascale-plus-rfsoc | read at step 1 |
| AMD UG1085 | Zynq UltraScale+ TRM: APU caches, PS DDR | https://docs.amd.com/r/en-US/ug1085-zynq-ultrascale-trm | read at step 1 |
| SimPy | Events, `Process.interrupt`, `Interrupt` | https://simpy.readthedocs.io/en/4.1.1/ | installed 4.1.2 |
| Vitis 2024.1 cross toolchain | `aarch64-linux-gnu-gcc` (a wrapper that adds `-mbranch-protection=none`), `-size`, `-nm`, `-objdump` | `/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/` | GCC 12.2.0, binutils 2.39 |

## 6. Environment

- **OS / shell:** Ubuntu 24.04.4 LTS, Linux 6.17, bash. 8 cores, 31 GB RAM, 121 GB free on `/home`.
- **Languages and runtimes (versions):** Python 3.12.3 (`/usr/bin/python3`) in the clone's own venv
  `/home/wirelesslab914/ali/waveflow-cpu/.venv`, created in step 0. It lacks the original venv's XSI
  additions (`XILINX_VIVADO`, `xsi_compat`), which this plan doesn't use. Host `gcc`/`g++` 13.3.0.
- **Frameworks and key dependencies (versions):** as resolved by `pip install -e ".[dev]"` from
  `main`'s `pyproject.toml` (which now needs `mcp>=2.3,<3`); step 0 records the resolved versions of
  simpy, numpy, pandas, scikit-learn and mcp in §15. The planning-time venv had simpy 4.1.2, numpy
  2.5.3, pandas 3.0.6 and scikit-learn 1.9.1. gem5 v25.1.0.1 and McPAT `74d4759f` are built in step 2.
  Docker 29.4.1 (the user is in the `docker` group). The Vitis 2024.1 cross toolchain (GCC 12.2.0)
  links static aarch64 binaries; this was tested on 2026-10-08.
- **Build / run / test commands:**
  - `cd /home/wirelesslab914/ali/waveflow-cpu && source .venv/bin/activate`
  - `pytest -m "not vitis and not xsi and not gem5"`: the usual loop. Before step 9 the `gem5` marker
    doesn't exist yet, so step 0's baseline uses `-m "not vitis and not xsi"`.
  - `pytest -m gem5`: the new marker; needs `~/ali/tools/gem5/build/ARM/gem5.opt`, `libm5.a`, Docker
    and the Vitis toolchain.
  - `ruff check waveflow/cpu examples/cpu_sched tests/cpu`; `black --check` on new files only;
    `mypy --follow-imports=silent waveflow/cpu`. Without `--follow-imports=silent`, mypy reports
    hundreds of errors from existing modules.
  - gem5 and McPAT run in the container; the exact commands are in step 2's details.
- **Project structure (the parts that matter):**
  - `waveflow/simulation/` (SimPy core), `waveflow/calib/` (models, platforms),
    `waveflow/hw/memif.py` (bus);
  - one directory per example under `examples/`, with a matching page under `docs/examples/`;
  - tests grouped by area under `tests/`, and `plans/`.

  pytest sets `pythonpath = ["."]`, and the editable install's finder sits behind the path finder.
  The clone's venv has its own editable install pointing at the clone, so `import waveflow` resolves
  to the clone from any directory (rule 14).
- **Limitations:** no sudo; no ARM board in this plan; the Arm Cortex-A53 TRM web page returns HTTP
  403 to the agent (gap accepted, §8); McPAT's smallest technology node is 22 nm (confirm in step 1).

## 7. Existing system and reuse

- **Current implementation (what it does, where):** no processor model exists. The hosts that "play
  the CPU" are `SimObj`s whose only timed actions are bus transfers; their own compute takes zero
  simulated time. They are:
  - `HistController` in `examples/shared_mem/hist.py`;
  - the regmap example's host;
  - the `Cpu` in `docs/guide/interface/axi_mm/modeling.md`.

  Hardware components follow a pattern this plan copies: a functional result computed at once, then
  `self.timeout(predicted)`. The prediction comes from a `CalibModel` that is seeded, fitted,
  persisted under a platform, and reported with a `Confidence`. `TimingModel` keeps everything
  internal in **cycles** and converts to seconds only at the boundary; do the same, with energy in pJ.
- **Reuse:**
  - `SimObj` (`process`, `timeout`, `event`) and SimPy events and `Process.interrupt`, for preemption.
  - `LinCalibModel`: `basis`, `target`, `seed`, `transform_fn`, `fit`, `predict_feat`, `rel_errors`,
    `max_rel_error`, `save_model` / `load_model` / `load_or_default`.
  - `CalibModel`'s `data_dir` / `corpus_path` / `params_path` and `CalibDataFrame`, giving the
    `TimingModel`-style corpus. If `data_dir` can't point at the platform's `cpu/` tree, copy
    `TimingModel`'s `calib_dir` layout and say so in §14.
  - `FitSummary.covers` / `outside` for `INTERPOLATED` vs `EXTRAPOLATED`.
  - `Confidence`, `ConfidenceLevel`, `Estimate` from `waveflow/calib/confidence.py`, built at report
    time.
  - `Platform.resolve(platforms_root, name, part=…, clk_freq=…, res_types=(…))`. `res_types` admits
    non-FPGA counters such as `area_mm2` and `leak_mw`.
  - `waveflow/toolchain/toolchain.py`'s `find_vitis_path` to locate the cross compiler.
  - gem5's HPI core and `starter_se.py`; McPAT's `ARM_A9_2GHz.xml` as the template to adapt.
  - The `xsi` session gate in `tests/conftest.py` as the pattern for a `gem5` gate.
  - tracerspecsense's task-group operations (`tg_add`, `tg_sort`, `tg_pri_change`, `tg_del`) as the
    *semantics* of the micro-scheduler, re-implemented small. Nothing is imported or copied wholesale.
- **Deliberately not reused, with the reason:**
  - **SimPy's `PriorityResource` / `PreemptiveResource`:** `SortedQueue.append` re-sorts the whole
    queue on every request. The plan review measured 218 tasks/s with 20,000 tasks queued, and it has
    no per-core identity for the switch charge. Use a `heapq` ready queue plus core tokens.
  - **`SimObj.action()` per task:** `_record_action` scans the whole history, which is O(N²).
  - **`ModuleStore` / `Record` / `Provenance`:** keyed by HwModule elaboration, with timing sources
    limited to `pysim` / `cosim` / `xsi`. The user chose the corpus instead.
- **Conventions to follow:**
  - the house docstring style: rationale-heavy module docstrings and `#:` attribute comments;
  - `@dataclass(kw_only=True)` components and `ProcessGen` typing;
  - names in cycles internally;
  - commits named after the plan step, e.g. `cpu(3): package skeleton`;
  - the `CLAUDE.md` rule that a measured number names its tool versions;
  - pre-registration before measuring, in the house style of `mimo_cg_paper_sims.md` 9.4a.
- **Docs to keep in sync:**
  - `docs/guide/`: the new `cpu/` section, linked from `docs/guide/timing_model/index.md` and from the
    `HistController` page `docs/examples/shared_mem/pysim.md`;
  - `docs/overview/status.md`: its "general-purpose processor" bullet;
  - `CLAUDE.md`: commands, architecture, the `gem5` marker.

## 8. Tools and access (readiness)

Checked 2026-10-08. Re-run before starting:

```bash
# guided-task plugin's readiness script (run via marketplace_env, see its SKILL.md):
export MARKETPLACE_ROOT=/home/wirelesslab914/ali/claude-marketplace
. "$MARKETPLACE_ROOT/common/scripts/skill_env.sh"
READY="$MARKETPLACE_ROOT/common/plugins/guided-task/skills/guided-task/scripts/check_readiness.py"
VB=/tools/Xilinx/Vitis/2024.1
"$PY" "$READY" --tool git --tool docker \
  --tool /home/wirelesslab914/ali/waveflow-cpu/.venv/bin/python \
  --tool $VB/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-gcc \
  --tool $VB/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-size \
  --path /home/wirelesslab914/ali/tracerspecsense/specsense \
  --path waveflow/calib --path docs/guide/timing_model \
  --writable plans --writable /home/wirelesslab914/ali/tools \
  --url https://github.com/gem5/gem5 --url https://github.com/HewlettPackard/mcpat \
  --url https://www.gem5.org/documentation/ --url https://developer.arm.com/documentation/ddi0500/latest \
  --git .
"$PY" "$READY" --url https://docs.amd.com/r/en-US/ug1085-zynq-ultrascale-trm \
  --url https://docs.amd.com/r/en-US/ds926-zynq-ultrascale-plus-rfsoc \
  --url https://github.com/gem5/gem5/blob/stable/configs/common/cores/arm/HPI.py
# Read-only probes run during planning (no credentials involved):
docker info --format 'server {{.ServerVersion}}'
for t in v25-1 v25-0 latest; do docker manifest inspect ghcr.io/gem5/ubuntu-24.04_all-dependencies:$t >/dev/null && echo "$t exists"; done
git ls-remote --tags https://github.com/gem5/gem5 | grep -oE "refs/tags/v2[0-9]\.[0-9]+(\.[0-9]+)*$" | sort -V | tail -3
$VB/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-gcc -O2 -static hello.c -o hello && file hello
```

| Check | Target | Status | Detail |
|---|---|---|---|
| tool | `git` | ✅ ready | git version 2.43.0 (/usr/bin/git) |
| tool | `docker` | ✅ ready | Docker version 29.4.1, build 055a478 (/usr/bin/docker) |
| tool | `/home/wirelesslab914/ali/waveflow/.venv/bin/python` | ✅ ready | Python 3.12.3 (~/ali/waveflow/.venv/bin/python) |
| tool | `/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-gcc` | ✅ ready | aarch64-xilinx-linux-gcc.real (GCC) 12.2.0 (/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-gcc) |
| tool | `/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-size` | ✅ ready | GNU size (GNU Binutils) 2.39.0.20220819 (/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-size) |
| path | `/home/wirelesslab914/ali/tracerspecsense/specsense` | ✅ ready | directory, readable |
| path | `waveflow/calib` | ✅ ready | directory, readable |
| path | `docs/guide/timing_model` | ✅ ready | directory, readable |
| writable | `plans` | ✅ ready | directory, writable |
| writable | `/home/wirelesslab914/ali/tools` | ✅ ready | does not exist yet — will be created (parent is writable) |
| url | `https://github.com/gem5/gem5` | ✅ ready | HTTP 200 |
| url | `https://github.com/HewlettPackard/mcpat` | ✅ ready | HTTP 200 |
| url | `https://www.gem5.org/documentation/` | ✅ ready | HTTP 200 |
| url | `https://developer.arm.com/documentation/ddi0500/latest` | ⚠️ check | HTTP 403 — reachable, but the agent needs credentials for it |
| git | `.` | ⚠️ check | branch `paper/mimo-cg`: 4 uncommitted change(s) — commit or stash them to create a checkpoint before starting |

**Readiness: READY** — 13 ready · 2 to check · 0 blocked

| Check | Target | Status | Detail |
|---|---|---|---|
| url | `https://docs.amd.com/r/en-US/ug1085-zynq-ultrascale-trm` | ✅ ready | HTTP 200 |
| url | `https://docs.amd.com/r/en-US/ds926-zynq-ultrascale-plus-rfsoc` | ✅ ready | HTTP 200 |
| url | `https://github.com/gem5/gem5/blob/stable/configs/common/cores/arm/HPI.py` | ✅ ready | HTTP 200 |

**Readiness: READY** — 3 ready · 0 to check · 0 blocked

Probe results (2026-10-08): Docker server 29.4.1; image tags `v25-1`, `v25-0`, `latest` exist; latest
gem5 tags `v25.1`, `v25.1.0.0`, `v25.1.0.1`; the static `hello` built as "ELF 64-bit LSB executable,
ARM aarch64, … statically linked".

| Capability | Kind (MCP server / plugin / skill / agent / device / person) | Purpose | Available? |
|---|---|---|---|
| Docker | tool | Build and run gem5 and McPAT without sudo | ✅ |
| gem5 v25.1.0.1 (`build/ARM/gem5.opt`, `util/m5` → `libm5.a`) | tool | Cycle ground truth | ❌ not yet: built in step 2 (accepted) |
| McPAT `74d4759f` | tool | Energy and area ground truth | ❌ not yet: built in step 2 (accepted) |
| Vitis 2024.1 aarch64 toolchain | tool | Cross-compile the C kernels | ✅ |
| Xilinx QEMU 8.1.0 | tool | Not used (functional only, no cycle timing) | ✅ (unused) |
| Web access (WebFetch / WebSearch) | tool | gem5, AMD and SimPy docs; a public source for interrupt latency | ✅ |
| Explore agent | agent | Broad codebase searches when a step needs one | ✅ |
| Reviewer agent (general-purpose) and `/code-review` | agent / skill | Plan review (done, §15); code review at close-out | ✅ |
| guided-task plugin | plugin | This plan's format and its `execute` mode | ✅ (`/home/wirelesslab914/ali/claude-marketplace`) |
| The user | person | Approves pre-registration (step 10), milestone pauses, review findings | ✅ |

**Gaps the user accepted (2026-10-08):**
1. **The Arm A53 TRM returns 403.** Workaround: take clock, caches and DRAM from AMD DS926 and
   UG1085 and the core model from gem5's `HPI.py`. Interrupt entry latency becomes a flagged constant
   cited from a public source, or `UNCALIBRATED`. Risk: that one constant is unverified (§14).
2. **gem5 and McPAT are not installed.** Workaround: step 2 builds both in Docker under
   `~/ali/tools/`. McPAT is built with the `-m32` flags overridden (§14). Risk: build time.
3. **Another agent is working in the original checkout.** Workaround: all work happens in the
   separate clone `/home/wirelesslab914/ali/waveflow-cpu` with its own venv; the original checkout and
   its venv are never touched.
4. **The original venv has mcp 1.30.0, but `main` needs `mcp>=2.3`.** Workaround: the clone-local venv
   (user, 2026-10-08). Risk: the network install in step 0 resolves newer package versions than those
   the planning-time numbers were taken with; they are recorded.

## 9. Approach

**Chosen approach:** a **loosely-timed processor model with calibrated cost models**, the
"host-compiled" or source-level timing style.
- **Execution:** software runs as Python at zero simulated time and returns work counters. A
  per-function `LinCalibModel` maps counters to cycles and to dynamic energy in pJ.
- **Scheduling:** the `Processor` keeps a `heapq` ready queue (priority, then arrival order) and one
  token per core. A granted task runs its Python body once, charges `switch + cycles` in one
  `timeout`, and on completion releases its result and its core.
- **Preemption:** `Process.interrupt` on the running task, with the remaining-cycles rule of step 5.
- **Ground truth:** a C twin of each kernel run on gem5's HPI core (an in-order Armv8-A model of the
  A53 class) in SE mode. Energy and area come from McPAT fed with the same runs' statistics.

This meets §2's accuracy bound at Python speed (§4 AC6, AC11). It is the same design as waveflow's
existing `TimingModel` and `BusTiming` (one LT philosophy, one calibration stack), and it keeps Python
as the single source of truth.

API sketch. This is a starting point: the doer may rename after reading the code, and records any
change in §14.

```python
@dataclass(kw_only=True)
class CpuConfig:                      # one DSE point
    name: str = "a53"
    n_cores: int = 1
    f_clk_hz: float = 1.2e9           # DS926 F_APUMAX at speed grade -1 (step 1)
    l1i_bytes: int = 32 * 1024        # confirm from UG1085 in step 1
    l1d_bytes: int = 32 * 1024
    l2_bytes: int = 1024 * 1024
    preemptive: bool = False
    platform: str | None = None       # where the calibrated models live

@dataclass(kw_only=True)
class SwFunction:                     # one piece of software the CPU runs
    name: str
    fn: Callable[..., tuple[Any, dict[str, int]]]   # returns (result, work counters)
    cycles: CalibModel                # counters (+ regime features) -> cycles
    energy_pj: CalibModel | None = None   # counters -> dynamic energy in pJ
    working_set: Callable[[dict], int] | None = None  # counters/args -> bytes

class Processor(SimObj):              # N cores, heap ready queue, optional preemption
    def execute(self, f: SwFunction, *args, prio: int = 0, **kw) -> ProcessGen[Any]: ...
    def interrupt(self, handler: SwFunction, *args) -> ProcessGen[Any]: ...   # high-priority task
    def report(self) -> CpuReport: ...  # records, queueing, utilization, energy, footprint, confidences

def cpu_area(config: CpuConfig) -> Estimate: ...   # mm² and leakage, with Confidence
```

**Search before building:**
- **SimPy resources** were considered for core arbitration and rejected: they don't scale, and they
  have no per-core identity (§7). SimPy still supplies events, timeouts and interrupts.
- **Calibration:** waveflow's calibration stack covers fitting, persistence and confidence.
- **Ground truth:** gem5's HPI core and `starter_se.py` cover the A53-class core.
- **McPAT template:** McPAT ships `ARM_A9_2GHz.xml`, which is out-of-order (`machine_type=0`), has no
  L2 (`number_of_L2s=0`) and targets 40 nm. It is the template to adapt.
- **gem5 → McPAT:** published converters target older gem5 statistics and the out-of-order core. Step
  11 searches them first and writes a small HPI-specific converter only if none fits.
- **Prior art:** tracerspecsense's `GenProcPE` / `Resource` / `Scheduler` is reference, not a
  dependency.

Alternatives considered:

| Option | Pros | Cons | Fits the requirements? |
|---|---|---|---|
| **Calibrated LT cost model (chosen)** | Python speed; same design and stack as the hardware timing models; confidence-reported; per-platform swappable | Accuracy only as good as the counters and the ground truth; cache and contention effects need explicit features | Yes: §2 speed and accuracy, single source of truth |
| Instruction-set simulator in the loop (QEMU, Unicorn, Renode) | Runs real binaries | Counts instructions, not cycles, so a timing model is still needed; the software must be C (breaks the single source); SimPy synchronization overhead | No: fails "no ISS in the loop" and gains no timing accuracy |
| gem5 co-simulation in the loop | Highest fidelity | Roughly 10⁵ instructions/s; heavy synchronization; makes a DSE sweep infeasible | No: fails the speed constraint |
| `ops / fclk` (tracerspecsense `GenProcPE`) | Trivial | Hand-counted ops, CPI fixed at 1, no caches, no confidence, no calibration | No: the DSE plan exists to replace it |

## 10. Steps

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 0 | In the clone: create the branch, create the venv, commit this plan, record the test baseline | the clone's `main`; this plan file | Clone on `feat/cpu-model`; the plan committed as `cpu(0)`; `.venv` installed; `waveflow` imports from the clone; baseline counts in §15 | `git branch --show-current`; `git log --oneline -1`; `python -c "import waveflow; print(waveflow.__file__)"`; `pytest -m "not vitis and not xsi" -q` | commit (the plan) | ☑ |
| 1 | Confirm the facts the model is configured from | DS926, UG1085, gem5 `HPI.py` / `starter_se.py`, McPAT README and `mcpat.mk` | A53 clock (xczu48dr speed grade), L1/L2 sizes, PS DRAM type and the closest gem5 DRAM model, HPI defaults, how `starter_se.py` sets clock, caches and DRAM (`-P` overrides or a config script), McPAT's minimum node; each with a citation in §14 | The §14 rows with links | commit (§14 edit) | ☑ |
| 2 | Build gem5 v25.1.0.1 (`ARM`), `libm5.a` for arm64, and McPAT in Docker under `~/ali/tools/` | §5 sources, step 1 | `hello` runs under HPI in SE mode at the step-1 clock and prints `hi`; `stats.txt` has `simTicks` and the core's cycle count; an m5-marked region dumps a stats block; McPAT runs its shipped ARM example | The commands in *Step details*; outputs pasted in §15 | — (outside the repo) | ☑ |
| 3 | Package skeleton: `waveflow/cpu/` with `CpuConfig`, `SwFunction`, `Processor` (heap ready queue, core tokens, one core, run-to-completion), `TaskRecord`; the no-example-imports test | §7 reuse list; §9 sketch | One task on one core charges exactly `switch + cycles`; imports are clean (AC1) | `pytest tests/cpu/test_skeleton.py tests/cpu/test_no_example_imports.py` | commit | ☑ |
| 4 | Scheduling: N cores, priority, FIFO ties, switch charge by core identity, queueing delay, utilization, `report()` with lazily computed confidence | step 3 | ≥ 8 hand-computed timelines pass (AC2, first part) | `pytest tests/cpu/test_scheduling.py` | commit | ☑ |
| 5 | Preemptive mode and interrupt-as-task | step 4; SimPy `Process.interrupt` docs | ≥ 4 more exact timelines pass, including the rounding and preempted-during-switch cases (AC2 complete) | `pytest tests/cpu/test_preemption.py` | commit | ☑ |
| 6 | Cost binding: seeded cycle and pJ-energy models over counters + regime features; static power; footprint; area-model interface | steps 3–5; `waveflow/calib/` | Seeds predict; `UNCALIBRATED` reported; regime features exact at the L1D/L2 boundaries; `report()` energy exact (AC4 seeded, AC7 report part, AC9 regime part) | `pytest tests/cpu/test_confidence.py tests/cpu/test_footprint.py tests/cpu/test_report.py` | commit | ☑ |
| 7 | Bus coexistence test and the speed bench `python -m waveflow.cpu.bench` (stream and burst patterns) | steps 3–6; `MMIFMaster` | AC3 passes; both patterns ≥ 20,000 tasks/s, recorded (AC11, first part) | `pytest tests/cpu/test_bus_coexistence.py`; the bench output in §15 | commit | ☑ |
| 8 | Calibration kernels: C sources + Python twins (scheduler ops, Q15 streaming, indexed gather) + overhead microbenchmarks | §9; tracerspecsense `tg_*` semantics | Twins bit-exact with host-compiled C on a smoke grid (AC5, host part, smoke grid) | `pytest tests/cpu/test_kernel_twins.py` | commit | ☑ |
| 9 | gem5 runner: cross-compile, run in Docker, warm-up + measured region, subtract the empty-region overhead, parse `stats.txt`, write a corpus row with provenance and `code_bytes`; refuse unregistered points; `gem5` marker and session gate | steps 2, 8 | The parser passes on a committed `stats.txt` fixture; the smoke points run end to end; an unregistered point is refused; `-m gem5` cannot skip silently (AC9 binary part) | `pytest tests/cpu/test_gem5_runner.py`; `pytest -m gem5 tests/cpu` | commit | ☑ |
| 10 👁 | **Pre-register** the platform, the per-kernel **fit / validation / test** sets, and the area configuration sets | steps 1, 8, 9 | `$PLATFORM/cpu/sweep_plan.csv` and `area_plan.csv` committed **before** any non-smoke gem5 or McPAT run; the user has reviewed them | `git log` shows the commit; the runner's refusal test passes; the user's go | commit; **pause for review** | ☑ |
| 11 | Run the sweep on gem5 and McPAT; fit on the fit set; settle model structure on the validation set; evaluate the test set once; write `accuracy.csv` | step 10 | AC4 (calibrated), AC5 (all points), AC6, AC7 (models) and AC12 hold, or the deviation policy ran on validation data and the user decided | `pytest tests/cpu/test_platform_accuracy.py tests/cpu/test_confidence.py tests/cpu/test_kernel_twins.py tests/cpu/test_provenance.py tests/cpu/test_preregistration.py` | commit | ☑ |
| 12 | Area and leakage models from McPAT over the configuration sets | steps 10, 11 (the McPAT converter and template) | AC8 holds | `pytest tests/cpu/test_platform_accuracy.py -k area` | commit | ☑ |
| 13 | Example `examples/cpu_sched/`: the micro-scheduler on `Processor` dispatching tasklets to `SimpFun` instances over AXI-Lite | steps 3–12; `examples/regmap/simp_fun.py` | The example runs; reports per-operation latency, queueing, utilization, energy and footprint with confidences; its test passes | `pytest tests/examples/test_cpu_sched.py` | commit | ☑ |
| 14 | Replay validation: C replay of the example's scheduler trace on gem5, one measured region per operation, against the model | step 13 | AC10 holds; the ≥ 1,000× speedup is recorded (AC11 complete) | `pytest -m gem5 tests/examples/test_cpu_sched_replay.py` | commit | ☐ |
| 15 | Cross-configuration report (informational): predict a second cache configuration without re-fitting, compare with gem5 | steps 11, 14 | Errors tabulated in `$PLATFORM/cpu/cross_config.csv` and summarized in the docs | The table; no threshold | commit | ☐ |
| 16 | Docs: `docs/guide/cpu/`, `docs/examples/cpu_sched/`; update `docs/overview/status.md`, the `timing_model` and `shared_mem` cross-links, `CLAUDE.md`, and the documented-numbers guard | all prior | AC13 holds | `pytest tests/docs tests/cpu/test_docs_present.py` | commit | ☐ |
| 17 | Regression and lint | all prior | AC14 holds against the step-0 baseline | The AC14 commands, output in §15 | commit (fixes only) | ☐ |
| 18 | Independent code review, agreed fixes, completion report, lessons | all prior | Review findings answered; §16 written; status set | The review output; §16 | commit | ☐ |

Milestones (each ends with a pause for the user's review and go):
- **M0 = steps 0–2:** environment and ground-truth tools.
- **M1 = steps 3–7:** the processor model, with no gem5 needed.
- **M2 = steps 8–12:** the calibration harness and the A53 platform.
- **M3 = steps 13–15:** the example and system validation.
- **M4 = steps 16–18:** docs and close-out.

Flags: ⚠️ irreversible (see §12): none in this plan · 👁 the user reviews the diff before continuing (step 10, plus each milestone pause).

### Step details

**Step 0: branch, venv and baseline.** Never touch the original checkout or its venv.

```bash
cd /home/wirelesslab914/ali/waveflow-cpu
git switch main && git pull --ff-only          # plan written against 12350fa4; record the commit used
git switch -c feat/cpu-model
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -c "import waveflow; print(waveflow.__file__)"   # must print .../waveflow-cpu/waveflow/__init__.py
git add plans/cpu_model.md && git commit -m "cpu(0): add the plan"
pytest -m "not vitis and not xsi" -q 2>&1 | tail -5      # record passed/failed/skipped in §15
```

`.venv` must be gitignored; check with `git status` before committing. If `waveflow` resolves
anywhere but the clone, stop: that is a deviation (rule 4).

**Step 1: facts.** Record each value with its source in §14:
- the A53 maximum clock for the xczu48dr's speed grade on the RFSoC 4x2 (DS926; the board part is
  expected to be `XCZU48DR-2FFVG1517E`);
- the APU's L1I/L1D size per core, the shared L2 size, and the PS DRAM type (UG1085 and the board),
  plus the closest DRAM model gem5 v25.1 offers;
- HPI's default cache sizes and latencies (`HPI.py`, expected 32 KiB / 32 KiB / 1024 KiB);
- what `starter_se.py` accepts (`--cpu hpi`, `--cpu-freq` with a default of 4 GHz, so always set it,
  `--num-cores`, a DRAM option with a default of `DDR3_1600_8x8`) and how cache sizes are overridden.
  Use `-P` parameter overrides, or a config script `waveflow/cpu/calib/gem5_cfg/a53_se.py` derived
  from `starter_se.py` if `-P` can't reach them;
- McPAT's minimum technology node and its in-order setting (`machine_type=1`).

**Step 2: tools.** On any build failure, apply the deviation policy before trying alternatives.

```bash
mkdir -p ~/ali/tools && cd ~/ali/tools
git clone --branch v25.1.0.1 --depth 1 https://github.com/gem5/gem5 gem5
git clone https://github.com/HewlettPackard/mcpat mcpat && git -C mcpat checkout 74d4759f3ba2dff8f5a69e07a68efdb46b42fb8c
IMG=ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1
docker pull $IMG
RUN="docker run --rm -u $(id -u):$(id -g) -v $HOME/ali/tools:/work -v /tools/Xilinx:/tools/Xilinx:ro"
XC=/tools/Xilinx/Vitis/2024.1/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-
$RUN -w /work/gem5 $IMG scons build/ARM/gem5.opt -j8                              # ~30–60 min
$RUN -w /work/gem5/util/m5 $IMG scons arm64.CROSS_COMPILE=$XC build/arm64/out/m5 build/arm64/out/libm5.a
$RUN -w /work/mcpat $IMG make CXX=g++ CC=gcc -j8    # overrides mcpat.mk's `-m32` (lines 25-26); pre-authorized
```

- **Smoke test:** inside the same image, run
  `/work/gem5/build/ARM/gem5.opt /work/gem5/configs/example/arm/starter_se.py --cpu hpi --cpu-freq $A53_CLK …` (the step-1 clock)
  on a static `hello`, and on a static program linked with `libm5.a` that marks an empty region. Then
  run `mcpat -infile ProcessorDescriptionFiles/ARM_A9_2GHz.xml -print_level 5`.
- **If `libm5.a` will not build with that syntax** (check `util/m5/README.md` at the tag): fall back to
  the **differencing method**. Run each point at two repetition counts and difference the totals, so
  start-up cancels out, and record the choice in §14.
- **If McPAT still will not build with the overrides:** stop and report. Don't switch forks without
  the user.

**Step 3–7: the model.**
- **Read first:** `waveflow/simulation/simobj.py`, `waveflow/calib/calib.py` (`CalibModel`,
  `LinCalibModel`, `CalibDataFrame`), `waveflow/calib/confidence.py`, `waveflow/calib/timing_model.py`,
  and `waveflow/hw/memif.py` (`MMIFMaster`).
- **Copy the test template:** `git show paper/mimo-cg:tests/linalg/test_no_example_imports.py` is the
  model for `tests/cpu/test_no_example_imports.py`. Also forbid `specsense` imports.
- **Ready queue:** a `heapq` of `(prio, seq, task)`. Each core is a token with an index and the id of
  its last task. A free core takes the head; a submit wakes an idle core. Never use SimPy's
  `PriorityResource` / `PreemptiveResource` (§7). Never call `SimObj.action()` per task; keep
  `TaskRecord`s in a list.
- **Python first, then charge:** a task's Python body runs **once**, when it is first granted a core.
  Its side effects happen then. Its result is released only at final completion, so nothing
  downstream sees it early.
- **Switch charge:** charged when a core's last task id differs from the granted task's.
- **Preemption rule:** each running segment records `work_start`, the time its switch charge ended.
  - On preemption at `now`:
    `executed = floor((now − work_start) · f_clk)` if `now ≥ work_start`, else `0`.
    In the else case the preemption hit during the switch, and the partial switch is lost as overhead.
  - Then `remaining = left − executed`.
  - The task re-enters the ready queue with `remaining` cycles at its original priority and `seq`, and
    is charged a switch on resume.
  - The tests pin this rule, including the rounding.
- **Confidence:** compute it in `report()`, cached per (function, feature-range), never per execute.
  The `Estimate` convention is to build confidence at report time.
- **Regime features:** for working set `ws`, `ws_over_l1 = max(0, ws − l1d_bytes)` and
  `ws_over_l2 = max(0, ws − l2_bytes)`.
- **Bench:** `waveflow/cpu/bench.py --pattern {stream,burst} --tasks N --cores 4`.
  - `stream` uses Poisson arrivals at 80 % offered load, so the queue stays bounded.
  - `burst` submits all N at `t = 0`, which exercises the heap at depth N.
  - Both print tasks/s, measured as wall time around `env.run()`. AC11 records the numbers. Don't
    hard-gate speed in the unit suite, where load makes it flaky; the test only asserts that the bench
    runs.

**Step 8: kernels** (`waveflow/cpu/calib/kernels/`, one `.c` and one Python twin each; every measured
function `__attribute__((noinline))`):
- **(a) `sched_ops`:** a ready list of task groups with priorities. Operations `add` (greedy
  admission), `sort` (priority order plus backfill), `reprio` and `delete`. Counters include
  `n_tasks`, `n_scanned`, `n_moved` and `n_assigned`.
- **(b) `cdot_q15`:** complex dot products over `n` samples in **Q15**: int16 inputs, int32 products,
  and int64 accumulation, with no overflow for the registered `n`. A defined final rounding and shift
  is mirrored exactly in the twin. Compute-bound and cache-friendly.
- **(c) `gather_hist`:** an indexed gather into a `uint32` histogram, with a working set swept across
  L1, L2 and DRAM. Memory-bound. The twin masks to C's unsigned wraparound.
- **(d) Overhead microbenchmarks:**
  - `ctx_switch`: a hand-written aarch64 cooperative switch saving and restoring x19–x30, sp and
    d8–d15, with no syscall. This is the primary measurement.
  - `swapcontext`: measured for information only, because of the SE-mode syscall caveat.
  - `dispatch`: a dequeue plus a call through a function pointer.
- **I/O:** each C program takes its features and a data seed on `argv`, runs one **warm-up** call on
  the same data, then wraps the measured call in `m5_reset_stats(0,0)` / `m5_dump_stats(0,0)`. It
  prints its outputs and counters as one JSON line.
- **Twins:** the Python twin computes the same outputs and counters with the same algorithm. Any
  integer overflow is masked to C semantics.

**Step 9: runner** (`waveflow/cpu/calib/gem5.py`):
- **Compiler:** locate it via `find_vitis_path`, overridable by `WAVEFLOW_AARCH64_GCC`.
- **Build:** `-O2 -static`, linking `libm5.a`. Record `code_bytes` for each measured symbol from
  `aarch64-linux-gnu-nm -S`. The binaries are build output, so gitignore them.
- **Run:** in the image `WAVEFLOW_GEM5_IMAGE` (default `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1`),
  with gem5 at `WAVEFLOW_GEM5_ROOT` (default `~/ali/tools/gem5`), at the step-1 clock, caches and DRAM.
- **Empty-region overhead:** measure an empty region once per configuration and subtract it from
  every point; record it as `empty_region_cycles`.
- **Corpus row:** parse the dumped stats block into `{cycles, insts, l1d_misses, l2_misses, …}`.
  Compare the program's JSON output with the twin (`output_matches_twin`). Append to the platform's
  the kernel's corpus (e.g. `cpu/sched_ops/corpus.csv`) with these provenance columns: `source=gem5`, `gem5_tag`, `gem5_commit`,
  `compiler`, `compiler_version`, `cflags`, `cpu_model`, `f_clk_hz`, `l1i`, `l1d`, `l2`, `dram`,
  `cache_state=warm`, `kernel_sig` (sha256 of the C source, flags and gem5 configuration), and
  `prereg_commit`.
- **Pre-registration guard:** refuse any point other than the named smoke points unless it is in
  `sweep_plan.csv`, and that file is tracked and unmodified (`git status --porcelain` is empty for it).
- **Session gate:**
  - register a `gem5` marker in `pyproject.toml`;
  - give `tests/conftest.py` a gate modeled on the `xsi` one, with `WANT_GEM5_GATES` recorded the same
    way, so that under `-m gem5` a skip fails the session;
  - run Python subprocesses with the clone's venv interpreter (rule 14).

**Step 10: pre-registration** (👁):
- **Platform:** create it with `Platform.resolve(waveflow/calib/platforms, name, part="cortex-a53 (gem5 HPI)", clk_freq=$A53_CLK, res_types=("area_mm2", "leak_mw"))`.
  Suggested name: `a53_hpi_1200mhz_gem5v25_1`, with the clock part changed if step 1 finds a different
  A53 clock.
- **Sweep plan:** `cpu/sweep_plan.csv` has one row per (kernel, feature point, data seed, role ∈ {fit,
  validation, test}).
  - The **fit** set spans each family's full feature range, including its corners.
  - **Validation** and **test** points are strictly **interior** to that range, so they report
    `INTERPOLATED`.
  - Both are **stratified**: each family's range and each cache regime (both sides of the L1D and L2
    boundaries) get points. This is the stratification lesson in `mimo_cg_lessons.md` on
    `paper/mimo-cg`.
  - Microbenchmarks: fit runs at one set of repetition counts and placements; validation and test at
    others.
- **Area plan:** `cpu/area_plan.csv` covers `n_cores ∈ {1,2,4}` × `L1 ∈ {16,32,64} KB` ×
  `L2 ∈ {256,512,1024,2048} KB`, with fit at the edges and interior configurations split between
  validation and test.
- **Commit and stop:** commit, then pause for the user's review.

**Step 11: sweep and fit.**
- **Converter:** search for an existing gem5 → McPAT converter that handles MinorCPU/HPI statistics.
  If none fits, write `waveflow/cpu/calib/mcpat.py`, which maps HPI stats into a McPAT XML adapted
  from `ARM_A9_2GHz.xml`:
  - set `machine_type=1` (in-order);
  - add an L2 block (`number_of_L2s=1`) with the A53 L2 size;
  - set the L1 sizes and the clock;
  - set `core_tech_node` to McPAT's smallest node.

  Report the node next to every number.
- **Fit:** on the **fit** rows, fit cycles with a `LinCalibModel` per kernel family (and a constant
  per overhead benchmark), and dynamic energy (pJ) with a `LinCalibModel` over the same counters.
  Take static power per configuration from McPAT.
- **Model structure:** on a miss, settle it on the **validation** rows only.
  - Hypothesis 1: a missing regime feature.
  - Hypothesis 2: a family that needs a piecewise fit per regime.

  Test each on validation, then stop (deviation policy).
- **Test:** evaluate the **test** rows **once**. Write `cpu/accuracy.csv` with per-point predicted,
  measured and relative error, and per-family median and max, for cycles and energy, with the
  evaluation's commit.

**Step 13: example.**
- **Arrivals:** a seeded Poisson stream of task groups, as in tracerspecsense's random-signal
  simulation.
- **The CPU's work:** the scheduler operations are `SwFunction`s calibrated in step 11. Dispatch
  launches `SimpFun` instances through their regmap over a `DirectMMIF` and waits for completion.
  Bus time comes from the existing bus model.
- **Outputs:** the trace of scheduler operations with their counters goes to `results/` (gitignored),
  plus a small committed fixture of at most 2,000 operations for the replay test.
- **If `SimpFun` can't be instantiated N times cleanly:** use a minimal `HostActivated` stub in the
  example and record why in §14.

**Step 14: replay.**
- **The program:** a C program replays the fixture's operations back to back, with warm caches like
  step 9. Each operation runs in its own measured region.
- **Stats volume:** if per-operation dumps are too heavy, dump only the core's cycle and instruction
  statistics (check `--stats-root` or the equivalent in v25.1). Failing that, measure every k-th
  operation and state k.
- **Comparison:** the model's per-operation prediction against gem5's, and the totals.
- **Speed:** record both wall times (AC11).

## 11. Verification strategy

- **Per step:** each step's *Verify with* in §10.
  - **Exact checks:** hand-computed timelines for semantics and energy accounting; bit-exactness for
    the twins against host C and against gem5's output.
  - **Accuracy:** committed tables plus a test that asserts the bounds on the test set.
  - **Gates:** gem5-marked integration tests that cannot skip silently.
  - **Pre-registration:** a guard in the runner plus a test.
  - **Hygiene:** `ruff` / `black` / `mypy --follow-imports=silent` on new code.
- **Regression check:** `pytest -m "not vitis and not xsi and not gem5"` compared with the step-0
  baseline (no new failures), plus `pytest -m gem5` with 0 skipped, plus `pytest tests/docs`.
  `-m vitis` and `-m xsi` are not run: this plan touches no HLS or RTL path. If `tests/conftest.py`
  changes, run `pytest -m xsi --collect-only -q` to show the `xsi` gate still collects its recorded
  count.
- **Independent review:**
  - **Plan:** done on 2026-10-08 by a reviewer subagent; 14 findings, all applied (§15).
  - **Result:** `/code-review` (high) on the branch diff at step 18; findings go to the user, who
    decides what to fix.

## 12. Safety

- **Version control:** branch `feat/cpu-model` in the clone `/home/wirelesslab914/ali/waveflow-cpu`,
  created from `main` in step 0 (not during planning). Commit per step, named after the step, e.g.
  `cpu(3): package skeleton`. Never commit in, stash in, or switch the original checkout.
- **Diff review by the user:** at each milestone pause. Explicitly flag:
  - any change to shared code: `waveflow/calib/`, `waveflow/simulation/`, `tests/conftest.py`,
    `pyproject.toml` markers;
  - the pre-registration CSVs (step 10);
  - `CLAUDE.md`.
- **Irreversible or destructive actions:**

  | Action | Step | Backup / recovery | Dry run | Approval |
  |---|---|---|---|---|
  | None expected. The Docker image and `~/ali/tools/` builds are removable (`docker rmi`, deleting the directories), but ask before deleting anything. | — | git for repo changes; re-clone or re-build for tools | — | at the time of the action |

- **Permissions:** write only inside the clone and `~/ali/tools/`. Read the paths in §5. Network
  only for `git clone`, `docker pull` and documentation. Docker containers mount `~/ali/tools`
  read-write and `/tools/Xilinx` read-only, and run as the user's uid. `pip install` only into the
  clone's `.venv`, in step 0.
- **Secrets:** none. `WAVEFLOW_GEM5_ROOT`, `WAVEFLOW_GEM5_IMAGE`, `WAVEFLOW_MCPAT_ROOT` and
  `WAVEFLOW_AARCH64_GCC` are optional path overrides, not secrets.
- **Deviation policy:** investigate the root cause, test up to two hypotheses, then stop and report.
  For accuracy, hypotheses are tested on the validation set only (rule 11). If the plan itself is
  wrong, propose the change, and update §10 and §14 after approval.

## 13. Agents and models

| Role | Agent / model | Responsibility |
|---|---|---|
| Doer | Claude Code main session (Claude Opus 5.5 or newer), run as `/guided-task execute plans/cpu_model.md` | Steps 0–18 |
| Searcher | Explore subagent, only when a step needs a broad sweep | Read-only searches; the doer keeps the conclusions |
| Plan reviewer | general-purpose subagent (done 2026-10-08) | Checked this plan against the checklist and the code on `main` |
| Result reviewer | `/code-review` (high) | Reviews the branch diff at step 18 |

## 14. Decisions, assumptions, risks, open questions

| Kind | Item | Resolution / owner |
|---|---|---|
| Decision | Complexity L3. | user, 2026-10-08 |
| Decision | Ground truth is gem5 (HPI core, SE mode), not a board or QEMU. QEMU counts instructions, not cycles, and no board is in scope. | user, 2026-10-08 |
| Decision | The first calibrated core is the Cortex-A53 (xczu48dr APU). | user, 2026-10-08 |
| Decision | Cost axes: timing, area, energy/power, memory footprint. | user, 2026-10-08 |
| Decision | Priority scheduling with optional preemption, a context-switch cost, and interrupts as high-priority tasks. Not a full RTOS model. | user, 2026-10-08 |
| Decision | Package `waveflow/cpu/`, with `tests/cpu/` and `docs/guide/cpu/`. | user, 2026-10-08 |
| Decision | Worked example: a micro-scheduler dispatching to an accelerator (`examples/cpu_sched/`). | user, 2026-10-08 |
| Decision | Area and energy come from McPAT fed with gem5 statistics. | user, 2026-10-08 |
| Decision | gem5 and McPAT are built in Docker from source at pinned versions, under `~/ali/tools/`. | user, 2026-10-08 |
| Decision | Context-switch and dispatch cost come from SE-mode microbenchmarks. Interrupt entry latency is a flagged constant. | user, 2026-10-08 |
| Decision | Accuracy bound: median ≤ 10 %, max ≤ 25 %, measured on the pre-registered test set. | user, 2026-10-08 |
| Decision | Branch off `main` (originally a worktree; now a separate clone, below). Pause at milestones. Deviation: two hypotheses, then stop. Review the plan now and the result at the end. | user, 2026-10-08 |
| Decision | A53 facts come from AMD UG1085 / DS926 and gem5 `HPI.py`, because the Arm TRM is unreachable. | user, 2026-10-08 |
| Decision | No branch is created during planning; step 0 creates it at execution. | user, 2026-10-08 |
| Decision | Work happens in a separate clone, `/home/wirelesslab914/ali/waveflow-cpu`, not a worktree, because another agent is working in the original checkout. The plan was moved there. | user, 2026-10-08 |
| Decision | The clone gets its own venv (`pip install -e ".[dev]"`), because `main` now needs `mcp>=2.3` and the original venv has 1.30.0. | user, 2026-10-08 |
| Decision | **Alignment with `plans/host_runtime.md`: standalone, adapter later.** M1 builds `Processor` as planned (`execute`, its own ready queue), without touching `host_runtime`'s files. Once `SwThread` / `SwHost` land on `main`, a small adapter lets `compute()` draw on a `Processor`. Accepted risk: two overlapping APIs until the adapter exists; the adapter is a follow-up, not part of this plan's ACs. | user, 2026-10-08 (M0 pause) |
| Decision | Measurements are a `TimingModel`-style corpus with provenance columns under the platform's `cpu/` directory. The record store is not extended. | user, 2026-10-08 (review finding 7) |
| Decision | Pre-registration uses fit / validation / test sets. Model-structure changes look at validation only; test is evaluated once; validation and test points are interior and stratified. | user, 2026-10-08 (review finding 8) |
| Decision | The numeric kernel is Q15 fixed point, so twins are bit-exact without disabling the compiler's multiply-add fusion. | user, 2026-10-08 (review finding 9) |
| Decision | All other review corrections are applied: the preemption rule, the heap ready queue, lazy confidence, the `libm5` syntax, the McPAT build override, energy in pJ, the mypy flag, the measurement method, step–AC mapping, `not gem5` in the dev loop, paths on `main`, and the `pip -e` and `black` rules. | user, 2026-10-08 ("apply all") |
| Decision | McPAT is built with `make CXX=g++ CC=gcc`, overriding `mcpat.mk`'s `-m32` (pre-authorized). Its `ARM_A9_2GHz.xml` template is adapted: in-order, an L2 added, smallest node. | user, 2026-10-08 (review finding 4) |
| Decision | The primary context-switch benchmark is a hand-written cooperative register switch with no syscall. `swapcontext` is informational, because SE mode emulates `rt_sigprocmask` at near-zero cost. | planner, from review finding 10 |
| Decision | Measurements use warm caches (one warm-up call), with the empty-region overhead subtracted, matching the back-to-back replay. | planner, from review finding 10 |
| Decision | The kernel suite is 3 families (scheduler operations, Q15 streaming, indexed gather) plus the overhead microbenchmarks, so that branchy, compute-bound and memory-bound code are all covered. | planner; veto at plan approval |
| Decision | The §2 accuracy bounds also apply to energy and area against McPAT. The user set them for cycles against gem5; extending them is the planner's choice. | planner; veto at plan approval |
| Decision | The measured region is marked with m5ops (`libm5.a`). The fallback is differencing two repetition counts. | planner; recorded in step 2 |
| Decision | Cost models are fitted by **relative-error least squares** (weights 1/measured^2, `RelLinCalibModel`), because the acceptance criterion is relative and ordinary least squares let the largest points set every fit. Chosen on the validation report (hypothesis 1 of 2), before the test set was evaluated; the model forms are as first registered in `calibrate.FAMILIES`. | doer, 2026-10-08 (step 11) |
| Decision | **AC6 accepted with a documented miss.** On the one-time test, `sched_ops.add` (max 0.393), `sched_ops.delete` (max 0.290) and `dispatch` (max 0.381) miss the 25 % max bound on small operations; their medians (0.047, 0.033, 0.011) and every other family pass, and all energy models pass. The models are kept; the miss and the small-operation limitation are recorded in the docs and pinned by `tests/cpu/test_platform_accuracy.py`, so a later recalibration that changes them is visible. A fresh registration with denser small sizes is a possible follow-up. | user, 2026-10-08 (step 11) |
| Decision | Calibration is single-core. Shared-L2 and memory contention between cores is **not** modeled; cores interact only through waveflow's bus model. | planner; the DSE plan revisits it |
| Decision | The cycle models are calibrated at one cache configuration, and regime features carry them to others. Step 15 measures how far that holds; per-configuration re-calibration is one harness command. | planner |
| Assumption | gem5's HPI approximates an A53-class in-order core but is **not** validated against silicon. The user chose gem5 knowing it is an approximation. "Accuracy" here means agreement with gem5, not with an A53. | user accepted, 2026-10-08 |
| Assumption | SE mode excludes OS effects (scheduler ticks, page faults, Linux context switches). The model therefore represents bare-metal or RTOS-style software. | user accepted, 2026-10-08 |
| Assumption | McPAT figures are reported at its smallest node (expected 22 nm), with the node named. No scaling to the A53's 16 nm is claimed. | user accepted (McPAT option), 2026-10-08 |
| Risk | McPAT still fails to build with the overrides. | Deviation policy; stop before switching forks |
| Risk | No maintained gem5 → McPAT converter handles HPI/MinorCPU statistics. | Step 11 searches first; a small converter is in scope |
| Risk | Accuracy misses at cache boundaries. | Regime features or a piecewise fit, decided on validation; stop if both fail |
| Risk | Per-operation stats dumps in the replay are too heavy. | Restrict the dumped stats; else sample every k-th operation and state k |
| Risk | The gem5 build takes 30–60 min and several GB. | 121 GB free; run it in the background; record the time |
| Risk | `paper/mimo-cg` (114 commits not on `main`) merges into `main` later, with calibration-stack changes. | `waveflow/calib/calib.py` differs by 2 lines today; rebase at the end if `main` moves; report conflicts |
| Risk | The speed bench varies with machine load. | Record the machine and the load; a script, not a hard unit-test gate |
| Risk | The interrupt entry latency constant has no verified source. | Flagged in code and docs; `UNCALIBRATED` if no public source is found |
| Open question | Where and on what was tracerspecsense's scheduler-delay table (`specsense/scheduler.py:1319`) measured? Useful as a sanity check on step 13's magnitudes; not blocking. | user |
| Open question | Does the later DSE need shared-L2 and memory contention between cores? | the DSE plan's owner |
| Fact (step 1) | **The RFSoC 4x2 carries `XCZU48DR-1FFVG1517E`, speed grade -1**, not the -2 this plan expected ([fpgadeveloper board page](https://boards.fpgadeveloper.com/boards/RFSoC-4x2); DigiKey's part listing). DS926 Table 1: `F_APUMAX` = **1200 MHz at -1**, 1333 MHz at -2 ([DS926 PS performance](https://docs.amd.com/r/en-US/ds926-zynq-ultrascale-plus-rfsoc/Processor-System-PS-Performance-Characteristics)). So `$A53_CLK` = **1.2 GHz** and the platform name becomes `a53_hpi_1200mhz_gem5v25_1`. Note: the repo's `rfsoc4x2_bfm_250mhz` platform and 40 other references use the part string `xczu48dr-ffvg1517-2-e` (-2); that is a pre-existing mismatch, reported to the user, not changed here. | doer, 2026-10-08 |
| Fact (step 1) | APU caches (UG1085 v2.5): L1I 32 KB 2-way and L1D 32 KB 4-way per core, L2 1 MB 16-way shared ([UG1085](https://docs.amd.com/r/en-US/ug1085-zynq-ultrascale-trm)). PS DRAM: DDR4, single 64-bit controller, max 2400 Mb/s at -1E (DS926 Table 3). gem5 model chosen: `DDR4_2400_8x8`, `--mem-channels 1` (the board's DRAM device organization is not confirmed; x8 is an assumption). | doer, 2026-10-08 |
| Fact (step 1) | gem5 v25.1.0.1 `HPI.py`: `HPI_ICache` 32 KiB 2-way (1/1/1-cycle latencies), `HPI_DCache` 32 KiB 4-way with a `StridePrefetcher`, `HPI_L2` 1024 KiB 16-way (13/13/5): **identical to UG1085's A53 caches**, so no cache override is needed for the reference configuration. `starter_se.py`: `--cpu hpi`, `--cpu-freq` (default **4GHz**, always set it), `--num-cores` (1), `--mem-type` (default `DDR3_1600_8x8`), `--mem-channels` (default 2), `--mem-size` (2GiB); **no cache-size options**, so other cache configurations (step 15, area grid) need `-P` overrides or `gem5_cfg/a53_se.py`. `util/m5/README.md`: `scons arm64.CROSS_COMPILE=<prefix> build/arm64/out/m5`; arm64's default m5op call type is `instruction`. | doer, 2026-10-08 |
| Fact (step 1) | McPAT @ `74d4759f`: `cacti/technology.cc` supports 180/90/65/45/32/**22 nm** (the 16 nm branch is commented out), so energy and area are reported at 22 nm. `mcpat.mk` lines 25–26 set `CXX = g++ -m32`, `CC = gcc -m32` (override confirmed needed). `ARM_A9_2GHz.xml`: `number_of_L2s=0`, `core_tech_node=40`, `clock_rate=2000`, `machine_type=0` (to change to 1 for in-order). | doer, 2026-10-08 |

## 15. Progress log

| Date | Step | What happened | Evidence | Deviation |
|---|---|---|---|---|
| 2026-10-08 | plan | Plan drafted with the guided-task plugin. Readiness READY (13 + 3 ready, 2 checks resolved as accepted gaps). No overlapping plan exists. | §8 | none |
| 2026-10-08 | plan review | An independent reviewer subagent checked the plan against the checklist and the code on `main`. It found 14 issues, 2 of them blocking: the preemption formula used SimPy's `usage_since` (a timestamp) as elapsed time, and the SimPy resource queue doesn't scale (218 tasks/s with 20,000 queued). The planner verified 5 of the claims on `main` and SimPy 4.1.2. The user chose to apply all corrections plus three decisions (corpus storage, fit/validation/test, Q15). Applied. | the reviewer's report; §14 decisions | none |
| 2026-10-08 | plan | The user approved the written plan; status set to Ready. No branch, worktree or tool install was made during planning. | the user's approval | none |
| 2026-10-08 | plan | After approval, the planner noticed the plan file is absent on `main`, so the worktree wouldn't have it. With the user's yes, step 0 now copies the plan into the worktree and commits it first. | the user's answer | none |
| 2026-10-08 | plan | The user moved the work to a separate clone (another agent is working in the original checkout). The plan was moved to the clone and adapted: clone instead of worktree, a clone-local venv (the user's choice, because `main` now needs `mcp>=2.3`), base `main` @ `12350fa4`. That `main` brought `plans/host_runtime.md`, which overlaps this plan; its alignment is a decision for the M0 pause, before M1. | the user's instructions; §14 | the readiness gate is re-run with the clone's paths in step 0 |
| 2026-10-08 | 0 | Clone on `feat/cpu-model` from `main` @ `12350fa4`; `.venv` from `pip install -e ".[dev]"` (simpy 4.1.2, numpy 2.5.3, pandas 3.0.6, scikit-learn 1.9.1, mcp 2.3.0, pytest 9.1.1, ruff 0.16.10, black 26.10.0, mypy 2.4.0); `import waveflow` resolves to the clone from `/tmp`; plan committed `60de56cf`. **Baseline** `pytest -m "not vitis and not xsi" -q`: **3868 passed, 40 skipped, 6 failed**, all pre-existing on `main`: `tests/build/test_rtl_module.py::test_shipped_memory_is_the_witness_plus_the_published_latency` (line endings), `tests/examples/test_markov_figures.py::test_every_manifest_figure_is_committed_and_recorded` (missing `docs/examples/markov/images/sync_status.json`), `tests/examples/test_markov_figures.py::test_the_committed_figure_is_what_the_build_renders` (stale `chain_output.svg`), `tests/examples/test_vitis_fft.py::test_generate_writes_the_build_tree` (no Vitis 2025.1 FFT headers), `tests/mcp/test_retrieval_eval.py::test_generated_code_is_excluded_by_default` and `tests/mcp/test_usage_index.py::test_generated_files_are_excluded_from_usage_by_default` (no generated files in a fresh clone). The first attempt was stopped at 18 % when the session restarted; the rerun completed. | the commands' output | the venv replaces the shared one (user, §14) |
| 2026-10-08 | 1 | Facts confirmed and recorded in §14. Surprise: the RFSoC 4x2 is speed grade -1, so the A53 clock is 1.2 GHz (not 1.333) and the platform is `a53_hpi_1200mhz_gem5v25_1`; the repo's `-2` part strings are a pre-existing mismatch, left alone. gem5 HPI caches equal UG1085's A53 caches. | §14 Fact rows with links | clock and platform name changed (as the plan allowed) |
| 2026-10-08 | 2 | Tools built under `~/ali/tools/` in `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1`: gem5 v25.1.0.1 (`c8222cc`) `build/ARM/gem5.opt` with `-j6` in 88 min (16:14–17:43, machine load ~16 on 8 cores); `util/m5` `libm5.a` via `scons arm64.CROSS_COMPILE=<Vitis prefix>`; McPAT `74d4759` via `make CXX=g++ CC=gcc` (no source change). Smoke tests, `starter_se.py --cpu hpi --cpu-freq 1.2GHz --num-cores 1 --mem-type DDR4_2400_8x8 --mem-channels 1`: static `hello` prints `hi`; CPU clock period 833 ps (1.2 GHz); an m5-marked program dumps one stats block per region: **empty region 94 cycles**, a 1000-iteration volatile loop 6044 cycles. McPAT on `ARM_A9_2GHz.xml`: 40 nm, 5.39698 mm², 0.108687 W leakage. | `~/ali/tools/{gem5_build,m5_build,mcpat_build,mcpat_smoke}.log`; the stats above | build took longer than the 30–60 min estimate (shared machine) |
| 2026-10-08 | 3 | `waveflow/cpu/` (`config.py`, `task.py`, `processor.py`, `__init__.py`): `CpuConfig`, `SwFunction`, `TaskRecord`, `Processor` with a `heapq` ready queue keyed `(prio, seq)` and per-core tokens (the switch charge is skipped when a core resumes the task it ran last), the body run once at first grant and the result released at completion, `compute(cycles)` and `interrupt()` entry points. Tests: `tests/cpu/test_skeleton.py` (one task charges `switch + cycles`, clock conversion, body-once/result-waits, negative prediction clamped, config validation) and `test_no_example_imports.py`: 9 passed. ruff, black (new files only, 88 columns: the repo configures neither) and `mypy --follow-imports=silent waveflow/cpu` clean. | `pytest tests/cpu -q` | none |
| 2026-10-08 | 4 | `report.py`: `CpuReport` (per-core busy time and utilization over a horizon), `FunctionStats` (count, cycles, mean/max latency and queueing delay), confidence computed in `report()`, once per distinct (model, features) point, the weakest call's level per function; a fixed numeric cost reports `UNCALIBRATED`. `tests/cpu/test_scheduling.py`: 11 hand-computed timelines (FIFO and switch per task, priority order, FIFO ties by arrival, two cores and lowest-index grant, first task pays a switch, idle time and utilization, queueing delay = latency - busy, per-function grouping, interrupt entry overhead, confidence INTERPOLATED -> EXTRAPOLATED, fixed cost UNCALIBRATED). `tests/cpu`: 20 passed; ruff, black, mypy clean. | `pytest tests/cpu -q` | none |
| 2026-10-08 | 5 | Preemption in `Processor`: at submit, a task still queued (in preemptive mode, or any interrupt) interrupts the least urgent running task if strictly more urgent; one victim per arrival, never a core already being preempted. `_preempted`: `executed = floor((now - work_start) * f_clk + 1e-6)`; during the switch nothing executes, the partial switch is charged and lost and the core holds no context; a remainder below 1e-6 cycles completes instead of re-queueing; the task re-queues at its original `(prio, seq)`. `tests/cpu/test_preemption.py`: 8 hand-computed timelines (preempt and resume, run-to-completion contrast, preempted during switch, interrupt preempts without preemptive mode, equal priority never preempts, least urgent victim on two cores, fractional cycle is not progress, resumed task ahead of later equals). AC2: 19 timelines in total. `tests/cpu`: 28 passed; ruff, black, mypy clean. | `pytest tests/cpu -q` | none |
| 2026-10-08 | 6 | Cost binding: `eval_cost` (numbers and models, clamped at 0); `TaskRecord.energy_pj` from `SwFunction.energy_pj` at first grant; `CpuConfig.static_power_mw` per core; `CpuReport.dynamic_pj` / `static_pj` (`n_cores * mW * s * 1e9`) / `total_pj`; per-function `energy_pj`, `code_bytes`, `ws_max`, cache `regime`, and an energy confidence beside the cycle confidence; `area.py`: `CpuAreaModel` (`area_mm2`, `leak_mw`, each a number or a model) over `config_features` (`n_cores`, `l1i_kb`, `l1d_kb`, `l2_kb`, `f_mhz`), returning `Estimate`s. Tests: `test_confidence.py` (seeded cycle, energy and area -> UNCALIBRATED; AC4 seeded part), `test_footprint.py` (regime features and flag on both sides of the L1D and L2 boundaries; AC9 regime part), `test_report.py` (energy = dynamic + static exactly; AC7 report part). `tests/cpu`: 43 passed; ruff, black, mypy clean. | `pytest tests/cpu -q` | none |
| 2026-10-08 | 7 | `tests/cpu/test_bus_coexistence.py` (AC3): a host runs `f`, writes 64 words through an `MMIFMaster` over a `DirectMMIF` (`latency_write=20`) to a slave charging one 100 MHz cycle per word, then runs `g`. CPU records are identical with and without the write, and the run is longer by exactly the bus time, (20 + 64) / 100 MHz, charged once; the core is free during the transfer. `waveflow/cpu/bench.py` and `tests/cpu/test_bench.py` (runs only, no speed gate). **AC11 first part:** `python -m waveflow.cpu.bench --pattern stream --tasks 100000` -> **40,259 tasks/s** (32.675 ms simulated, 2.484 s wall); `--pattern burst --tasks 20000` -> **37,115 tasks/s** (0.539 s wall); 4 cores, Python 3.12.3, Intel i7-7700K (8 threads), load average ~9 at the time. `tests/cpu`: 48 passed; ruff, black, mypy clean. | the bench output above; `pytest tests/cpu -q` | the first version of the test failed: an unconfigured `DirectMMIF` charges zero time (lesson) |
| 2026-10-08 | 8 | `waveflow/cpu/calib/kernels/`: `wf_kernel.h` (xorshift32, `key=value` args, one-line JSON, `WF_ROI_BEGIN/END` = m5ops under `-DWF_GEM5`, empty on the host) and `sched_ops.c` (add/delete/reprio/sort on a `(prio, id)`-sorted list; counters `n_tasks`, `n_scanned`, `n_moved`), `cdot_q15.c` (Q15, int64 products: the first draft summed two int16 products in int32, which overflows at -32768^2*2, fixed before any run), `gather_hist.c` (uint32 bins, working set 4m), `dispatch.c`, `ctx_switch.c` (hand-written AAPCS64 switch of x19-x30, d8-d15 and sp, no syscall; ucontext on non-aarch64 hosts) and `swapcontext.c` (informational); twins in `sched_ops.py` and `numeric.py`; the `KERNELS` registry with each kernel's args, counters, working set, smoke points and `sw_function()`. All six build `-Wall -Wextra` clean for the host and, with `-DWF_GEM5` and `libm5.a`, for aarch64. On gem5 (HPI, 1.2 GHz): `ctx_switch k=100` measured 16,375 cycles for 200 switches (~81 per switch net of the 94-cycle empty region); `sched_ops op=add n=10` 309 cycles, output identical to the host build. `tests/cpu/test_kernel_twins.py`: 37 smoke points bit-exact against host gcc (AC5 host part, smoke grid). `pyproject.toml`: `waveflow.cpu` package data for the `.c`/`.h` files (shared file, flagged for review). `tests/cpu`: all pass; ruff, black, mypy clean. | `pytest tests/cpu -q` | none |
| 2026-10-08 | 9 | `waveflow/cpu/calib/gem5.py`: `Gem5Config` (HPI, 1.2 GHz, `DDR4_2400_8x8` x1; non-HPI cache sizes refused until step 15), `Gem5Runner` (cross gcc found through `find_vitis_path`, overridable by env; build cached by `kernel_sig`; gem5 run in the dependency image with gem5 mounted read-only; the first stats block is the region; `cycles = cycles_raw - empty_region_cycles`; `code_bytes` from `nm -S`, counting GCC clones such as `tg_remove.isra.0`; provenance columns), `empty.c` (empty region), `prereg.py` (`SweepPlan`: tracked, unmodified, roles, the adding commit stamped as `prereg_commit`; the runner refuses unregistered points before running anything). `tests/conftest.py`: the session gate generalized per marker (`_GATES`), `WANT_GEM5_GATES = 9`; `-m xsi` still collects 161 (checked before and after). `pyproject.toml`: the `gem5` marker. Tests: unmarked `test_gem5_runner.py` (parser on a committed 33-line `tests/fixtures/cpu/stats_sched_ops_add_n10.txt`, config, guard on a throwaway repo, refusals, clone-aware symbol sizes); gem5-marked: empty region **94 cycles**, every kernel end to end (output equals twin), the guard with a plan, and `test_footprint_binary.py` (AC9 code-bytes part). **`pytest -m gem5`: 9 passed, 0 skipped, 79 s.** With `WAVEFLOW_GEM5_ROOT=/nonexistent` the session fails (exit 1, "9 of 9 gem5 gates SKIPPED"). Smoke readings (net of 94): `sched_ops add n=10` 210 cycles, `cdot_q15 n=100` 1,056, `gather_hist n=5000 m=4096` 40,144, `dispatch n=50` 948, `ctx_switch k=100` 16,281. `tests/cpu` unmarked: all pass; ruff, black, mypy clean on new code (`pandas` import marked untyped: no stubs in the dev deps). The existing `_skip_reason` line keeps its pre-existing ruff FURB188 finding (house code, unchanged). | `pytest -m gem5 -q -rs`; `pytest tests/cpu -m 'not gem5'` | `code_bytes` first failed on `tg_remove.isra.0` (lesson) |
| 2026-10-08 | 10 | **Pre-registration committed, awaiting the user's review.** Platform `waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/` via `Platform.resolve(part="cortex-a53 (gem5 HPI)", clk_freq=1.2e9, res_types=("area_mm2", "leak_mw"))`. `cpu/sweep_plan.csv` (305 kernel points) and `cpu/area_plan.csv` (96 configurations) generated by `waveflow/cpu/calib/sweep.py`. Kernel points by fit / validation / test: sched_ops add 33/10/8, delete 33/10/8, reprio 33/10/8, sort 30/8/8; cdot_q15 20/5/6; gather_hist 21/5/6; dispatch 14/6/6; ctx_switch 5/3/4; swapcontext (informational) 3/0/2. Area: 80 edge configurations fit, 16 interior split 8 validation / 8 test. Every held-out point's counters and working set lie inside the fitted range (checked with the twins, no measurement), and seeds are disjoint per role. `tests/cpu/test_sweep_plan.py` pins the committed files to the generator. No non-smoke point has been measured. | `tests/cpu/test_sweep_plan.py`; `git log` | ctx_switch varies repetition count only: no data-placement variation (the plan's "placements" were not implemented; the coroutine stack is static) |
| 2026-10-08 | 10 | The user reviewed and approved the pre-registration (`76dd3655`) as committed. | the user's approval | none |
| 2026-10-08 | 11 | **Campaign:** all 305 registered points measured on gem5 in 13.5 min (4 workers), 0 failures, 0 twin mismatches; empty region 94 cycles. **Energy:** McPAT on every row (10 min); `energy_pj` net of the empty region's 337.0 pJ, all positive. **First fit (OLS) on fit rows, validation report:** 4 cycle models missed: `sched_ops.delete` (val max 0.295), `sched_ops.reprio` (max 0.268), `cdot_q15` (median 0.476, max 0.878; fit max 13.5), `gather_hist` (median 0.233); every energy model passed. **Root cause** (fit and validation data only): ordinary least squares minimizes absolute error, so the largest points (up to 15M cycles) set the fit; `cdot_q15` paid with a 2,083-cycle intercept and a 3.9 cycle/sample slope (true: ~11 in L1, ~12 in L2, 16.2 in DRAM), and the sched_ops misses were all n=3 points under an intercept set by large n. **Hypothesis 1** (the only one tested): fit the acceptance criterion, i.e. weight each point by 1/measured^2 (`RelLinCalibModel`); forms unchanged. **Validation after H1: every family passes**, cycles and energy (worst: `cdot_q15` cycles max 0.195, `sched_ops.sort` 0.164, `sched_ops.delete` 0.127). The area models use the same criterion, decided before any area data. Test set not yet evaluated. | `cpu/validation.csv`; the campaign log | one hypothesis used of two |
| 2026-10-08 | 11 | **Test set evaluated once** at `0fb7bead` (`cpu/accuracy.csv`, 112 predictions, all `INTERPOLATED`). **Energy: all 9 families pass** (worst max 0.091). **Cycles: 6 of 9 pass; 3 miss the max bound, all medians pass**: `sched_ops.add` med 0.047 **max 0.393**, `sched_ops.delete` med 0.033 **max 0.290**, `dispatch` med 0.011 **max 0.381**. The misses are small operations: add n=6 (157 measured vs 219 predicted, both seeds), dispatch n=32 (~990 vs 616), delete n=24 (~374 vs ~266). 6 of 56 cycle test points exceed 25 %, 41 of 56 are within 10 %. Validation had passed (its sizes 3/12/48/192/768 avoided these, two seeds per size). **AC6 is not met as written.** Per rule 11 the models are not changed against this test set; deviation policy: stopped and reported to the user for a decision. | `cpu/accuracy.csv` | AC6 missed on 3 cycle families (max bound only) |
| 2026-10-08 | 11 | Step closed on the user's decision (AC6 accepted with the documented miss). `waveflow/cpu/platform.py`: `CpuPlatform` loads a calibrated platform: per-family `sw_function()` (the twin priced by the fitted cycle and energy models, code bytes from the corpus), `switch_cycles()` = the `ctx_switch` slope (**81.8 cycles per switch**), `cpu_config()` (platform clock, switch cost, static power from the leakage model once step 12 fits it), `area_model()`. Tests: `test_platform_accuracy.py` (AC5 gem5 part: all 305 rows match their twins; AC6 medians all pass, maxima pass except the three accepted misses, which are pinned; AC7 energy all pass; all test predictions INTERPOLATED; each test point evaluated once), `test_provenance.py` and `test_preregistration.py` (AC12: every provenance column on all 305 rows, tools as named, no home paths; every row registered with its role and measured under the registration commit; each model saw exactly its fit rows; the test evaluation descends from the registration), `test_confidence.py` calibrated part (AC4), `test_kernel_twins.py` over all 305 registered points (AC5 host part). `tests/cpu` unmarked: 115 passed; ruff, black, mypy clean. | `pytest tests/cpu -m 'not gem5'` | AC6 as accepted |
| 2026-10-08 | 12 | McPAT (22 nm, idle) on all 96 registered configurations in 2 min 52 s (6 workers) -> `cpu/area/corpus.csv`. Area and leakage fitted on the 80 edge configurations (`n_cores` + `n_cores x (L1I + L1D KiB)` + `L2 KiB`, relative-error least squares), committed at `abb57967`, then the area test evaluated once at that commit. **Validation:** area med 0.040 max 0.056, leakage med 0.004 max 0.009. **Test (AC8): area med 0.044 max 0.063 PASS, leakage med 0.003 max 0.009 PASS**, all INTERPOLATED. `CpuPlatform.cpu_config()` now prices static power: 96.9 mW for 1 core (with its 1 MiB L2), 72.1 / 59.8 mW per core at 2 / 4 cores; area 3.07 / 4.12 / 6.20 mm². The first CLI run of the area stage failed: the area functions had been appended after the module's `__main__` guard (fixed, lesson). `tests/cpu/test_platform_accuracy.py` gains the area and static-power checks. | `cpu/area/accuracy.csv`; `pytest tests/cpu/test_platform_accuracy.py` | none |
| 2026-10-08 | 12 | **Correction:** the M2-closing commit `5e61f42a` went in with 2 failing tests. `test_platform_accuracy.py::test_twins_*` and `test_provenance.py::*` globbed `cpu/*/corpus.csv`, which since step 12 also matches `cpu/area/corpus.csv` (McPAT-only rows: no twin or gem5 columns, +96 rows). The commit command piped pytest into `tail`, which hid its exit code. Fixed: both tests read the kernel corpora by name. Re-run with exit codes printed: `pytest tests/cpu -m 'not gem5'` exit 0 (all pass); `pytest -m gem5` exit 0, **11 passed, 0 skipped**. | the two exit codes | a failing commit, corrected in the next one |
| 2026-10-08 | 13 | `examples/cpu_sched/cpu_sched.py`: a micro-scheduler on the calibrated A53 (`CpuPlatform`), ready list kept by the calibrated `sched_ops` algorithm on the real list (`add` on arrival, `delete` of the head on dispatch, `reprio` of the tail for aging, `dispatch` per hand-off), dispatching seeded Poisson jobs to N `SimpFun` accelerators over AXI-Lite (`DirectMMIF` 10-cycle latencies at 100 MHz) with interrupt completion. Default 0.3 us mean inter-arrival, 2 accelerators: 200 jobs in 137.8 us, all correct, CPU utilization 0.76, 16.1 uJ (2.8 dynamic, 13.4 static); `sched_ops.add` EXTRAPOLATED on the 35 arrivals that found an empty list (fit range starts at 1), INTERPOLATED on the other 165. **Found while building it:** (1) `Processor.execute` consumes `prio`, so the job priority now travels as `tg_prio` (documented in `execute`); (2) two races: the dispatch head and the aging target were chosen before the call held the core, so at high load two dispatchers took the same head (the sweep at 0.3 us hung: a stranded entry kept the aging loop alive) and aging re-inserted an already-dispatched job (199 of 200 run, one twice). Fixed by choosing inside the call, at grant; `run()` now raises unless every job is dispatched exactly once. `CpuReport` gains per-function counts of calls per confidence level (`FunctionStats.levels`). Tests: `tests/examples/test_cpu_sched.py` (4: once-only dispatch and correct results, every operation priced with non-UNCALIBRATED confidence, the trace re-applies to identical counters, determinism) and a levels test in `test_scheduling.py`. `tests/cpu` + the example tests: all pass (exit 0); ruff, black, mypy clean. | `pytest tests/cpu tests/examples/test_cpu_sched.py -m 'not gem5'` exit 0 | aging policy changed from oldest-arrival to the list's tail (the oldest-arrival scan is not a calibrated operation) |

## 16. Completion report

*Filled in when the work ends.*

- **Changed:** —
- **Tested (commands and results):** —
- **Acceptance criteria:** —
- **Decisions made:** —
- **Assumptions and not verified:** —
- **Remaining risks and issues:** —
- **Next steps:** —
- **Reusable artifacts saved / tools that would have helped:** —

## Checklist coverage

| Group | Addressed (where) | Not applicable (why) |
|---|---|---|
| T: task definition, planning | §1–§4 (goal, requirements, deliverables, AC1–AC14, each with its owning step); §9 alternatives; §10 steps and milestones; pause cadence in the rules | — |
| C: context and environment | §5 sources with access (paths checked on `main`); §6 environment; §7 existing system, reuse and deliberate non-reuse (C5, C6) | C4 (generate a repo overview): `CLAUDE.md` and `docs/guide/` already document the repo |
| V: verification | §4; §10 *Verify with*; §11; pre-registration (rule 11); deviation policy in the rules and §12 | — |
| D: documentation and continuity | Lessons file in the header; step 16 docs and `CLAUDE.md` (D2, D3); §16 report and reusable artifacts (D4, D5) | — |
| R: version control and safety | §12: separate clone, branch, commit per step, diff review areas | R3 destructive actions: none; tool builds are removable |
| S: security and permissions | §12 permissions (least privilege: the clone and `~/ali/tools/` only; installs only into the clone's venv) | S1 secrets: no credentials are needed |
| A: authoritative sources | §5 (gem5 v25.1, AMD DS926 / UG1085, SimPy 4.1, McPAT) and step 1 | — |
| P: plugins, MCP, tools | §8 capability table; §9 search before building | P3 MCP servers / `.mcp.json`: no MCP server is needed |
| M: multi-agent | §13; §11 independent review (plan review done) | — |
