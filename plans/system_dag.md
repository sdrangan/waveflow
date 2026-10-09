# Plan: a system's whole flow on one BuildDag -- csynth to the RTL trace gate

**Status:** drafted 2026-10-08.  Follows `plans/host_runtime.md` (S0-S7, merged in PR #238), which made
`run_system_xsi(sysm)` run a whole bus system at RTL from its pysim object.  This plan puts that run,
and everything before it, on a `BuildDag`, and retires `examples/markov/markov_xsi.py` and
`examples/mm_fir/mm_fir_xsi.py`.

## Motivation

`run_system_xsi` (`waveflow/build/system_xsi.py`) is one straight-line function: find the cut, check
the RTL is present and fresh, generate the crossbar IP / top / harness / scenario, run XSI, run pysim,
compare traces.  It does **not** build: csynth is each example's `*_build.py`, a hand-rolled loop
outside the DAG (`markov_build.synth`, `mm_fir_build.synth`), and the RTL check only names the script to
run.  Each example then keeps a `*_xsi.py` holding the scenario, the probes, the top / crossbar names,
the RTL paths and the result decoders.

Checking staleness and skipping what is fresh is what `BuildDag` is for.  On a DAG:

* `python -m examples.markov.markov_build` builds what changed and runs the system at RTL;
  `--through pysim` runs only pysim (no Vivado); `--status` says what is stale;
* the steps from csynth on are **framework**, derived from the system object, so a new bus system
  writes its codegen and its scenario and nothing else;
* the `*_xsi.py` files go: what in them is a choice moves next to the system, what decodes results
  moves into the gate tests, and the rest is derived.

## The shape: an outer DAG of six steps, two of them with inner DAGs

```
codegen ──> csynth ──┐
                     ├──> system_xsi ──┐
scenario ──┬─────────┘                 ├──> compare
           └──> pysim ─────────────────┘
```

| Outer step | Owner | Inner DAG | Produces |
|---|---|---|---|
| `codegen` | example | yes (as today: headers, kernel tops, writer tops, `.tcl`) | `include/`, `gen/` |
| `csynth` | framework | yes: one step per HLS top | `<top>_proj/` + stamp, per top |
| `scenario` | framework | no | `<work>/scenario/` (the host writes it) |
| `pysim` | framework | no | `<work>/pysim_traces/`, pysim cycles |
| `system_xsi` | framework | yes: crossbar IP → system top → host harness → XSI run | `<work>/traces/`, the report |
| `compare` | framework | no | the trace mismatches (empty: pass) |

Why these boundaries:

* **csynth is outside `system_xsi`** because other consumers need the RTL (resource reports, unit
  cosim), and it is the expensive step whose freshness matters most.
* **scenario is its own step** because pysim and XSI must read the *same* file.
* **pysim and `system_xsi` are siblings, `compare` consumes both**: `--through pysim` needs no Vivado.

**The rebuild unit is the HLS top.**  Vitis has no incremental csynth; each top is its own project.
Markov has four (`markov_gen`, `markov_chain`, the queue writer, the credit writer), joined only in the
system top, beside the non-HLS parts (crossbar IP, BRAM, MM adaptors -- framework Verilog or cached
IP).  So editing `markov_chain`'s body re-runs one csynth; the others' RTL is reused.  That is why the
csynth inner DAG has one step per top.

## Freshness: a per-step hook, decided late

`BuildDag` decides freshness by mtime (`_files_stale`), and cascades: a step whose upstream runs must
run (`_determine_must_run`, rule 4).  Both are wrong for csynth:

* `codegen` rewrites `include/` and `gen/` every run, usually with **identical bytes**; by mtime every
  csynth would always be stale.  This is the trap `waveflow/build/rtl_digest.py` was built to avoid --
  `rtl_staleness` compares a content stamp written at csynth time, because the mtime check silently
  skipped gates (history in `trace_steps.rtl_staleness`).
* The cascade marks csynth must-run before codegen has even run, so no pre-run check can see that
  the regenerated sources are the same.

The hook:

```python
class BuildStep:
    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool | None:
        """True / False to decide this step's freshness by content; None (default) for the mtime rule."""
        return None
```

* **Decided late.**  The DAG evaluates the hook *just before* the step would run -- after its upstream
  has run -- and skips the step if it returns True, even when the cascade marked it.  Downstream
  steps then see it as not having run (the cascade stops there).  `--force` / `--force-step` still
  win.
* **Csynth** answers with `rtl_staleness(root, top) is None` (the stamp, with its mtime fallback when
  there is no stamp -- never "clean").
* **A composite step** (one with an inner DAG) returns None and always enters; its inner DAG runs
  **without `force`** and each inner step decides for itself.  Entering costs milliseconds.  (Today's
  inner DAGs all run `force=True`, e.g. `bram_access_build.py`; that stays where codegen is seconds.)
* **`--status`** asks the hook too, so a csynth whose stamp matches reads fresh even when `gen/` is
  newer.
* The outer step **passes its inputs down**: the artifacts it consumes enter its inner DAG as
  `SourceStep`s, so inner steps see what changed (as bram_access's inner DAG does with its source).

Coarse outer steps cost little: `system_xsi` re-entering means seconds of regeneration (the crossbar IP
is cached by its config digest, `axi_xbar.generate_axi_xbar`) plus the XSI run, which re-runs anyway.

## Build or check: who may run csynth

`rtl_staleness` is deliberately a predicate: "a helper that silently runs the toolchain is its own kind
of surprise", and a 40 s csynth hidden in a test fixture is one.  So csynth has two modes, a
`BuildConfig` param:

* `synth="build"` (the CLI default): a stale top is synthesized, then stamped;
* `synth="check"` (the gate tests, and `run_system_xsi`): a stale or missing top **fails** the step,
  naming the top and the source that changed, as `run_system_xsi` does today.  A skipped XSI gate is a
  failure, so the gates keep failing loudly on stale RTL.

## The framework API

```python
# waveflow/build/system_dag.py
def add_system_steps(dag, sysm, *, work_dir, top=None, xbar_name=None, inside=None,
                     probes=None, prefix="") -> None:
    """csynth (one inner step per HLS top in the cut), scenario, pysim, system_xsi, compare --
    consuming the example's `codegen` artifacts.  `prefix` names a second system in the same DAG."""

class SystemXsiStep(BuildStep): ...      # the body of today's run_system_xsi, steps 3-4
class CsynthStep(BuildStep): ...         # one top: run_vitis_hls + write_stamp; is_fresh = the stamp
```

* `top` defaults to the system class's name in snake case (`MarkovSystem` → `markov_system`),
  `xbar_name` to `xbar_<top>`; both stay overridable (markov keeps `markov_top`, `xbar_markov_4x3`, so
  the crossbar cache and the docs hold).
* The csynth set is `system_top_spec(...).modules` -- the kernels **and** the bus-writer tops the cut
  brings in -- so `markov_build.WRITERS` stops being restated.
* The report is a file (`<work>/report.json`: the `XsiRun` fields), so the gate tests read it after
  the DAG runs.
* **`run_system_xsi(sysm, work_dir, ...)` stays**, as a thin wrapper: a DAG of the framework steps
  with `synth="check"`, run through `compare`, returned as an `XsiRun`.  Its callers
  (`tests/build/test_system_xsi.py`, the docs) do not change.

## An example after the change

```python
# examples/markov/markov_build.py
def build_dag(probes: bool = False) -> BuildDag:
    sysm = MarkovSystem(jobs=default_jobs(4, 300), link="mm")
    dag = BuildDag()
    dag.add(MarkovCodegenStep(name="codegen"))        # today's generate(), as an inner DAG
    add_system_steps(dag, sysm, work_dir="xsi_work", top="markov_top", xbar_name="xbar_markov_4x3",
                     probes=timing_probes(sysm) if probes else None)
    return dag

if __name__ == "__main__":
    run_dag_cli(build_dag, description=..., default_through="compare", root_dir=HERE, ...)
```

Where `markov_xsi.py` goes:

| Today | After |
|---|---|
| `TOPS`, `QWRITER`/`CWRITER`, `rtl_dir`, `system_spec`, `xbar_config`, `run_xsi` | deleted: derived, or the DAG |
| `system()`, `scenario_jobs()` (4 jobs × 300 steps) | `markov_build.py` |
| `timing_probes` | `markov.py`, next to `MarkovSystem` |
| `trace_report`, `job_results`, `parse_kv`, `probe_runs` | `tests/examples/test_markov_xsi.py` |

**mm_fir** has one HLS top (`mm_fir`) and two topologies (`per_view` → `xbar_mm4_1x4`, `one_front`
→ `xbar_mm1_1x2`): `add_system_steps` is called twice with `prefix="per_view_"` / `"one_front_"`,
sharing one `codegen` and one csynth.  That is the test that the framework steps are general.

## Stages

Each stage keeps `pytest -m "not vitis and not xsi"` green; Stages 2, 4 and 5 also run the relevant
`-m xsi` gates.  **The gates are the oracle**: mm_fir 618 / 611, markov 1870, bit-exact, host traces
identical to pysim's.  Never change `EXPECTED_CYCLES`; a moved count is a difference to find.

**Stage 1 -- the freshness hook.**  `BuildStep.is_fresh`, evaluated late in `BuildDag.run` (after
upstream, before the step; cascade stops at a step that reports fresh), honoured by `results_status`
and overridden by `force`.  Unit tests in `tests/build/test_build.py`: a step whose input is rewritten
with identical bytes is skipped; a changed byte runs it; the cascade stops; `--force` wins; a hook
returning None keeps today's behaviour exactly.

**Stage 2 -- csynth as a step.**  `CsynthStep(top)` (`run_vitis_hls` + `write_stamp`; `is_fresh` from
`rtl_staleness`; `synth="build" | "check"`), and a composite `csynth` step with one inner step per top.
Port `markov_build` and `mm_fir_build` csynth to it, keeping `generate()` callable (the gates call it).
Gate: csynth runs on a fresh tree, a second run skips all four markov tops, touching one kernel body
re-runs that top only; the xsi gates unchanged.

**Stage 3 -- scenario, pysim, compare.**  The three framework steps, from today's `run_system_xsi`
steps 5 and the host's `write_scenario`.  `--through pysim` produces traces without Vivado.  Gate:
pysim traces byte-identical to the ones `run_system_xsi` writes today.

**Stage 4 -- `SystemXsiStep` and `add_system_steps`.**  `run_system_xsi`'s steps 3-4 as an inner DAG
(crossbar IP, system top, harness, XSI run), the report as `report.json`, the defaults for `top` /
`xbar_name`, the csynth set from the cut.  `run_system_xsi` becomes the thin wrapper.  Gate: the
markov and mm_fir xsi gates through the wrapper, unchanged.

**Stage 5 -- the examples on their DAGs; retire `*_xsi.py`.**  `markov_build.py` and
`mm_fir_build.py` as above, on `run_dag_cli`.  Move the scenario, probes and decoders as in the table;
update `tests/build/test_system_top.py`, `test_sw_host_gen.py`, `test_xsi_system_top.py`,
`test_mm_fir.py`, `tests/docs/test_documented_numbers.py`; the gate tests run the DAG with
`synth="check"` and read `report.json`.  Delete `markov_xsi.py`, `mm_fir_xsi.py`.  Gate: full `-m xsi`
(`WANT_XSI_GATES` unchanged at 162 unless a test is merged, which is then recorded here).

**Stage 6 -- docs.**  `docs/examples/markov/xsi.md` and `rtlsim.md` rewritten around the DAG
(`--through`, `--status`, which steps are the example's); `docs/examples/mm_fir/rtlsim.md`,
`pysim.md`; `docs/guide/build/xsi_system.md` (the framework steps, the freshness hook, build vs
check); the other guide pages that name `run_system_xsi` or the `*_xsi.py` files
(`sw_threads.md`, `bfm_model.md`, `concurrent_flowsteps.md`, `axi_mm/crossbar.md`,
`axi_mm/slave_howitworks.md`).

## Open questions

* **Narrower stamps.**  The stamp hashes all of `include/`, so a schema header one top does not
  include still re-runs that top.  Correct, sometimes wasteful; narrowing means hashing each top's
  actual includes.  Not needed now.
* **Inner step names on the CLI** (`--through csynth_markov_chain`): possible, not built until wanted.
* **Codegen derived from the cut.**  The writer tops' codegen is framework (`write_writer_project`)
  and could move into `add_system_steps`; kernel codegen stays the example's (hand-written bodies,
  its schema list).  Decide in Stage 5.

## Progress log

### Stage 1 -- the freshness hook (2026-10-09)

`BuildStep.is_fresh(config, paths)` (default None); `paths` is the step's consumed and produced file
artifacts.  `BuildDag.run` keeps its pre-run must-run set exactly as before and revisits it just before
each step (`BuildDag._decide_late`):

* forced or a legacy `Buildable`: runs, the hook is not asked;
* the hook says True: skipped, even when the mtime rule or the cascade marked it; False: runs;
* None: the pre-run answer, except that a step marked **only** by the cascade is skipped when none of
  its upstream actually ran -- which only a hook can cause, so with no hook answering the late answer
  *is* the pre-run answer (`test_hook_none_matches_the_pre_run_answer` checks it step for step).

`results_status` asks the hook too (a new `hook` key per entry), so a fresh step does not make its
consumers stale.  Tests: `tests/build/test_build.py::TestFreshnessHook` (12); the 48 existing BuildDag
tests unchanged.

Deviations:

* **`Buildable.is_fresh` already existed**, with another signature (`(config, results)`), for its
  own callers.  Kept; the DAG never asks a `Buildable` (rule 2 always runs it), so the two cannot
  meet.  Documented on the method.
* **The hook's False is honoured too**, not only True: a step the mtime rule calls fresh runs when
  its hook says stale, and the cascade carries on from it (`test_hook_false_runs_a_step_...`).  The plan
  only described True; without this a csynth whose stamp disagrees but whose mtimes look old (a file
  restored by an older copy) would be skipped.
* **`waveflow/build/cli.py` changed** (one line of `--status` output): a step stale by its hook prints
  `STALE (the step's own check)`, a missing artifact `STALE (missing)` (it printed `STALE ( newer)`).
  (Wording settled in Stage 5.)
* **`--status` crashes under a cp1252 console** (`✓`), before this change too; run with
  `PYTHONIOENCODING=utf-8`.  Not fixed here.

Gate: `pytest -m "not vitis and not xsi"` green except `tests/mcp/test_knowledge_corpus.py::test_index_builds_in_under_three_seconds`, a wall-clock test that failed the same way on the
untouched baseline while other work loaded the machine, and passes alone.  It recurs below as "the
timing flake".

### Stage 2 -- csynth as a step (2026-10-09)

`waveflow/build/system_dag.py`: `CsynthStep(top)` (`run_vitis_hls` + `write_stamp`; `is_fresh` =
`rtl_problem(root, top) is None`, which is `rtl_staleness` with a missing RTL made a problem too, since
`rtl_staleness` leaves absence to its caller) and the composite `CsynthTopsStep` (`csynth`), one inner
`CsynthStep` per top, its consumed `include` / `gen` entering the inner DAG as `SourceStep`s, the inner
DAG run without `force`.  `synth="build" | "check"` is a `BuildConfig` param; in check mode a stale or
missing top raises (`"<top>: <why> (synth='check' does not run csynth: ...)"`) and the toolchain is
never called.  A failed csynth now carries the Vitis log tail (it was a bare `CalledProcessError`).

`markov_build.py` and `mm_fir_build.py` are DAGs on `run_dag_cli` (`codegen` -> `csynth`, default
`--through csynth`, `--synth build|check`); `generate()` stays callable (mm_fir gained one).  Markov's
docs figure is on its DAG too: `--through sync_docs_figures` replaces `--figures`, and
`MarkovFiguresStep.is_fresh` is False (it draws from Python the DAG cannot see).  `--no-synth` is
`--through codegen`; `--only <top>` is gone (inner step names on the CLI: the plan's open question).
mm_fir keeps its tracked `mm_fir.tcl` at the example root (`tcls={"mm_fir": "mm_fir.tcl"}`).

Gates:

* **Fresh tree** (a copy of `examples/markov` with only `src/`): all four tops synthesized, 123 s.
  **Second run**: `csynth` skipped whole (its hook: every top fresh), 0 s; codegen rewrote `include/`
  and `gen/` with identical bytes.  **A kernel body touched with identical bytes**: skipped.
* **"Touching one kernel body re-runs that top only" does not hold, and cannot with today's stamp.**
  A byte edit of `src/markov_chain_core_task.h` makes all four tops stale -- `markov_gen` and both
  writers too -- because a top's stamp hashes all of `src/**` and `include/*`
  (`rtl_digest.source_files`), not the files it includes.  The plan's "Narrower stamps" open question
  names `include/` only; `src/` has the same reach.  What does re-run one top only is a change to that
  top's own `gen/<top>.cpp` (`test_csynth_reruns_only_the_top_whose_source_changed`).  **Proposed:**
  narrow the stamp to each top's transitive `#include` closure (or let csynth record the files Vitis
  read); until then, an edit to one body costs every csynth (2 min for markov).  Not done here.
* **A fresh-tree csynth under a long path fails silently**: from the scratchpad
  (`C:/Users/.../AppData/Local/Temp/claude/<130 chars>/markov_fresh`) Vitis stopped after scheduling
  the queue writer's `GATHER` loop with `Synthesis failed.` and no error text; the same tree at
  `C:/wf_fresh_mkv` built.  Windows path length is the likely cause (Vitis's per-module paths are deep
  and the writer's module names long).  Not a DAG issue; noted for whoever builds in a deep directory.
* **mm_fir's tracked headers had drifted from their generators.**  The first `mm_fir_build` run
  regenerated `include/fir_cfg.h` (`w = 0;` after a full word), `include/int16_array.h` (TLAST only on
  the last word) and `mm_fir.tcl` (`-Isrc`): PR #240's array-aligned layout had never been regenerated
  into mm_fir's tracked copies, and the mm_fir gate never regenerates before its staleness check (the
  markov gate does).  The stamp saw the change and csynth rebuilt `mm_fir` (the plan's flow working as
  intended).  The regenerated files are committed with this stage.
* **XSI gates** (`test_markov_xsi.py`, `test_mm_fir_xsi.py`, `-m xsi`): 13 passed, 0 skipped --
  markov 1870, mm_fir 618 / 611 on the rebuilt `mm_fir` RTL, bit-exact, traces identical.
* Fast tests: `tests/build/test_system_dag.py` (11, Vitis replaced by a stand-in): fresh tree then
  skip, one top's source re-runs only it, a shared header re-runs all, check mode fails stale and
  missing tops without synthesizing and passes a fresh tree, a missing stamp falls back to mtime,
  `--status` reads a stamped top fresh under a newer `gen/`, the Vitis log on failure.
* Fast suite: exit 0, 3958 passed, 4 skipped (the timing flake passed this time).  This checkout's
  pytest prints no final count line; the exit code and the progress dots are the record.

### Stage 3 -- scenario, pysim, compare (2026-10-09)

`ScenarioStep` (`host.write_scenario` -> `<work>/scenario/`), `PysimStep` (the system run from that
file; `<work>/pysim_traces/` and `<work>/pysim.json` holding the cycle count in host clocks) and
`CompareStep` (`compare_traces` -> `<work>/compare.json`).  Each takes the system object, its
workspace and a `prefix` for its step and artifact names.

Deviations:

* **These steps are never fresh** (`is_fresh` returns False), and neither will `system_xsi` be.  The
  plan says a composite "returns None and always enters", but in this code None means the mtime rule,
  which skips a step whose outputs are newer than its inputs.  It would skip pysim after an edit to
  `markov.py`, because the DAG cannot see the Python a run reads.  False is what "always enters" needs.
  Every one of these is seconds, and the XSI run re-runs anyway.  csynth is the one outer step that
  decides freshness by content, and `CsynthTopsStep.is_fresh` is "every top fresh", not None, so a run
  with nothing to synthesize does not enter it.  The plan's "entering costs milliseconds" holds either
  way.
* **`compare` fails on a mismatch** after writing `compare.json`, so a CLI run whose host disagrees
  does not print PASSED.  `run_system_xsi` (Stage 4) reads `compare.json` and returns the mismatches
  as before, without raising.
* The pysim cycle count is a file of its own (`pysim.json`): the step that measures it is not the one
  that writes `report.json`.

Gate:

* `--through pysim` needs no Vivado, and its traces are **byte-identical** to the ones `run_system_xsi`
  wrote on main this morning (`tests/build/_xsi_work/{markov,mm_fir_per_view,mm_fir_one_front}/
  pysim_traces`, copied before any run on this branch).  The scenario bundles are identical too, and
  so are the RTL traces from those runs.
* `tests/build/test_system_dag.py`: the same comparison without the snapshot, against today's
  sequence (spec walk, scenario, harness render, `sysm.run()`), for all three systems, cycle count
  included.  Compare passes equal traces and fails a corrupted one.
* Fast suite: exit 0 (4035 passed, 4 skipped by the progress dots).

### Stage 4 -- `SystemXsiStep` and `add_system_steps` (2026-10-09)

`SystemXsiStep` (`system_xsi`) is an inner DAG `xbar_ip` -> `system_top` -> `harness` -> `xsi_run`,
the scenario entering as a `SourceStep`.  Each inner step hands the next an in-memory value, and
`xsi_run` calls `XsiWorkspace.prepare` with exactly the arguments `run_system_xsi` used.  The run is
written to `<work>/report.json` (`system_xsi.write_report`: the `XsiRun` fields) and read back by
`system_xsi.load_run`, together with `pysim.json` and `compare.json`.  `add_system_steps(dag, sysm,
work_dir=, top=, xbar_name=, inside=, probes=, prefix=, workspace=, sources=, tcls=, timeout=)`:

* `top` defaults to the class name in snake case (`MarkovSystem` -> `markov_system`), `xbar_name` to
  `xbar_<top>` (via `system_top_spec`);
* the csynth set is `system_top_spec(...).modules`, which for markov is the four tops `markov_build`
  generates, the two writers included (tested);
* a top whose `rtl_<top>` an earlier system's csynth already produces is consumed from it, so with
  `prefix` two topologies share one `codegen` and one `csynth` (`per_view_csynth` only; tested);
* a `sources` artifact with no producer in the DAG becomes a `SourceStep` under the root -- which is
  how `run_system_xsi` gets `include/` / `gen/` without a codegen step.

`run_system_xsi` is the thin wrapper: the DAG with `synth="check"`, through `compare` (or `system_xsi`
with `compare_pysim=False`), raising on any failed step but `compare` (whose mismatches are data:
`compare.json` is written before it fails, and is cleared at its start so a stale verdict cannot
outlive a crashed comparison).  Its signature and callers are unchanged.

Deviations:

* **`workspace` is a parameter of `add_system_steps`** (the plan's signature lacks it): mm_fir's two
  topologies share a top name, and the existing workspaces (`markov`, `mm_fir_per_view`, ...) keep
  their names.  Default `<prefix><top>`, `_probes` appended with probes, as `run_system_xsi` did.
* **pysim may now run before `system_xsi` walks the same system object** (the topological order puts
  `pysim` first; `run_system_xsi` walked, ran XSI, then ran pysim).  Guarded by
  `test_the_top_and_harness_do_not_depend_on_pysim_having_run` (all three systems: the generated top
  and harness are the same text either way) and by the gates below.
* Errors from a missing or stale top are now `RuntimeError` from the csynth check
  (`"markov_gen: no csynth RTL for markov_gen at ..."`); a missing top was a `FileNotFoundError`.

Gate (`-m xsi`, through the wrapper): `test_markov_xsi.py`, `test_mm_fir_xsi.py` and
`test_sw_channels_xsi.py` (also a `run_system_xsi` caller): **14 passed, 0 skipped** -- markov 1870,
mm_fir 618 / 611, the queued host 618, bit-exact, traces identical; `report.json`, `pysim.json`,
`compare.json` in each workspace.  Fast suite: green but for the timing flake, which also failed on
the untouched baseline.  It passes alone 3 times out of 3 and fails only inside the full ~4000-test
process, so it is pre-existing and not from this branch.  `tests/build/test_system_dag.py`: 28.

### Stage 5 -- the examples on their DAGs; `*_xsi.py` retired (2026-10-09)

`markov_build.build_dag(probes=False, work_dir="xsi_work")`: `codegen`, then `add_system_steps(dag,
system(), top="markov_top", xbar_name="xbar_markov_4x3", workspace="markov")`, and the figure steps;
`run_dag_cli(..., default_through="compare")`.  `mm_fir_build.build_dag(...)`: `codegen`, then
`add_system_steps` per topology with `prefix="per_view_"` / `"one_front_"` (workspaces
`mm_fir_per_view` / `mm_fir_one_front`, so the generated IP caches and the docs hold), sharing one
`csynth`.  Both CLIs take `--synth build|check` and `--probes`.  `markov_xsi.py` and `mm_fir_xsi.py` are
deleted:

| was in `*_xsi.py` | now |
|---|---|
| `TOPS`, `QWRITER`/`CWRITER`, `rtl_dir`, `RTL`, `system_spec`, `xbar_config`, `run_xsi` | gone: derived (`system_top_spec`, `rtl_rel`), or the DAG |
| `system()`, `scenario_jobs()`, `NJOBS`/`NSTEPS`; mm_fir's `system(topology)`, `scenario_x`, `PLAN`, `PKT`, `NSAMP`, `XBAR_NAMES` | `markov_build.py` / `mm_fir_build.py` |
| `timing_probes` | `markov.py` / `mm_fir.py`, next to the system class |
| `trace_report`, `job_results`, `parse_kv`, `probe_runs`, `output_words` | the gate tests |

The gate tests run the example's DAG with `synth="check"` through `compare` (mm_fir: through
`<topology>_compare`), read the run with `load_run`, and decode it.  A csynth check failure is now
`pytest.fail`, no longer a skip: a stale or missing top fails the gate and names it.  The DAG's
`codegen` runs first, so the mm_fir gate now regenerates its headers before the stamp check, as the
markov gate already did (the hole Stage 2 found).

Also updated: `tests/build/test_system_top.py`, `test_sw_host_gen.py`, `test_xsi_system_top.py`,
`test_system_xsi.py`, `test_system_dag.py`, `tests/examples/test_mm_fir.py`, and
**`tests/examples/test_sw_channels_xsi.py`**.  The plan's list missed that one: it imported `mm_fir_xsi`
and calls `run_system_xsi`.  It now takes the scenario from `mm_fir_build` and the decoders from
`test_mm_fir_xsi`.  It drops its staleness pre-check, since the wrapper's check mode fails a stale top.
`tests/docs/test_documented_numbers.py` needed no change (it reads `EXPECTED_CYCLES` from the gate file,
which kept it), but its symbol check fails while the example pages name `system_spec` / `xbar_config`,
and `test_markdown_integrity` fails on their links to the deleted files.  So the four pages it named
(`docs/examples/markov/xsi.md`, `markov/rtlsim.md`, `mm_fir/rtlsim.md`,
`guide/interface/axi_mm/crossbar.md`) go in with this stage, already rewritten.  So does the
guide page their links point into, `docs/guide/build/xsi_system.md`, whose new per-step anchors they
use.  Each commit stays green; Stage 6 is the remaining pages.

Framework changes:

* **`run_dag_cli` passes the parsed arguments to a `dag_factory` that takes one**, so a knob that
  changes the DAG's shape (`--probes`) can reach it; a zero-arg factory is called as before.
  `default_through=None` runs the whole DAG (mm_fir has two sinks).
* **The first csynth is named `csynth`**, not `<prefix>csynth`, so mm_fir's shared one reads as shared.
* `--status` wording: a missing artifact reads `STALE (missing)` before anything else; a hook's False
  reads `STALE (the step's own check)` (the always-run steps answer False without checking content).
* `examples/{markov,mm_fir}/xsi_work/` are gitignored.

Open question settled: **writer codegen stays the example's** (one `write_writer_project` call per
writer in `generate()`).  The csynth set is derived from the cut, so an example that forgot a writer
fails `csynth` on its missing `.tcl`.  Moving the writer codegen into `add_system_steps` would have it
write into the example's `gen/` from outside the example's `codegen` step, which is two writers of one
directory.  Revisit if a third example repeats the lines.

Gate: full `pytest -m xsi`: **162 passed, 0 skipped** (`WANT_XSI_GATES` unchanged at 162; no test
merged or removed).  Markov 1870, mm_fir 618 / 611, the queued host 618, bit-exact, traces identical.
Fast suite: exit 0 (4044 passed, 4 skipped).

### Stage 6 -- docs (2026-10-09)

* `docs/guide/build/xsi_system.md` (committed with Stage 5, see there): "Running it" is now the system on
  a build DAG -- the shape, `add_system_steps`, one section per framework step with its own anchor
  (`#csynth`, `#scenario`, `#pysim`, `#system-xsi`, `#compare`), build vs check, the freshness hook, and
  `load_run` / `run_system_xsi` as the one-call wrapper; "What it does not do yet" now lists the
  coarse stamp and the writer codegen, and drops "does not build".
* `docs/examples/markov/xsi.md` (Stage 5) is a walkthrough of the DAG, no longer of `markov_xsi.py`.
  It says which steps are the example's (`codegen`, the scenario), gives a table of the framework
  steps, each linked to its guide section, and covers the CLI (`--through pysim`, `--through csynth`,
  `--status`, `--synth check`, `--probes`), `load_run`, gates that run the DAG in check mode, and
  probes from `markov.py`.  Its two "Step 4" / "Step 5" references now name and link the sections of
  The system.  `markov/rtlsim.md`, `mm_fir/rtlsim.md` and `axi_mm/crossbar.md` (Stage 5) follow suit.
* This stage: `markov/codegen.md`, `index.md`, `pysim.md`, `theory.md` (`--figures` ->
  `--through sync_docs_figures`); `mm_fir/codegen.md` (`--no-synth` -> `--through codegen`),
  `mm_fir/pysim.md` (the scenario from `mm_fir_build`, probes via `--probes`);
  `guide/build/sw_threads.md`, `guide/custom_hooks/bfm_model.md`,
  `guide/flows/concurrent_flowsteps.md` (the pysim / compare / system_xsi steps, linked, where they
  said `run_system_xsi`).  `axi_mm/slave_howitworks.md` names only the gate test file, which stays:
  no change.

Gate: docs tests green; the fast suite run for Stage 5 already had every docs edit in the tree, with
no code change since (exit 0, 4044 passed, 4 skipped).

### After Stage 6 -- `system_rtl`, and the markov pages split by step (2026-10-09, the user's request)

Reviewing the docs, the user asked for the markov pages to follow the flow with one figure each:
codegen (what each pysim object generates), synthesis (what each becomes in RTL), the XSI testbench.
On those pages, "synthesis" spanned two DAG steps: csynth, and the first half of `system_xsi` (the
crossbar IP and the system top). So the DAG was changed to match, with the user's agreement:

* **`system_rtl`**, a new outer step between csynth and `system_xsi` (`SystemRtlStep`): an inner DAG of
  `xbar_ip` and `system_top`, writing `<work>/<top>.v` and `<work>/rtl.json` (every Verilog file the
  top compiles, and the IP's include directories).  It consumes the csynth'd tops' RTL;
  `system_xsi` (`SystemXsiStep`, now harness + run) consumes `rtl.json` and the scenario.  csynth and
  `system_rtl` are everything that makes Verilog, and `--through system_rtl` builds all of a system's
  RTL without simulating it.  `system_xsi` passes `XsiWorkspace.prepare` the same file list as before;
  the top is now written by `system_rtl` and no longer passed through `extra_files`.
* `add_system_steps` still returns the `system_xsi` step (`.rtl` is the `system_rtl` step), so
  `run_system_xsi` is unchanged.
* Docs: `docs/examples/markov/` gains `build.md` (Build flow: the DAG, the example's steps vs the
  framework's, the CLI) and `synth.md` (Synthesis: csynth, the crossbar IP, the top walked from the graph,
  Fig. 2). `codegen.md` gains Fig. 1, and `xsi.md` is now the testbench only (Fig. 3).  Pages run Build
  flow (6), Code generation (6.2), Synthesis (6.4), XSI testbench (6.6), RTL simulation (7).  The figures are
  one Mermaid graph of the pysim objects, the same layout each time, coloured by what the page
  produces (`classDef` with explicit text colours, so they read in both themes).  The guide gains
  `#system-rtl`; mm_fir's rtlsim and the step lists in docstrings name the new step.

Gate: the markov, mm_fir and queued-host XSI gates after the split: **14 passed, 0 skipped** (1870,
618 / 611, 618, traces identical).  `--through system_rtl` on markov: csynth UP-TO-DATE, `rtl.json` with
57 files.  Docs tests green.  Fast suite: exit 1 on two MCP wall-clock tests,
`test_index_builds_in_under_three_seconds` and `test_no_tool_call_stalls_inside_the_server` (the
latter passes alone).  The index build took 10-15 s on this branch, but **8-9.5 s on `main` at the same
time**, against under 3 s that morning, so the machine was slow, not the branch.  One real effect was
found and fixed: the knowledge index walked `examples/markov/xsi_work/` (the CLI's run directory --
crossbar IP Verilog, harness, traces).  `waveflow/mcp/knowledge/corpus.py` now skips `xsi_work` as it
skips `work`.

### Open after this plan

* **Narrow the source stamp** (Stage 2's finding): a body edit re-synthesizes every top.
* `--force` re-enters `csynth`, but its inner DAG runs without force, so a fresh top is not
  re-synthesized.  To force one, delete its `<top>_proj/rtl_sources.json`.  Inner step names on the
  CLI (`--force-step csynth_markov_chain`) would be the clean way.
* A deep working directory (long Windows paths) can fail csynth with no error text (Stage 2).
