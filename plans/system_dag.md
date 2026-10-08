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

(empty)
