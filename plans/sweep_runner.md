# SweepRunner — one place for "run this design at every point and keep the measurements"

> **Sequenced after `plans/integration_record.md`.**  That work changes what a sweep summary should
> contain, so capturing a byte-identical golden of today's `sweep.json` first would pin a format this
> plan intends to change.  See [What the summary is for](#what-the-summary-is-for).

## Why

Two examples sweep a parameter grid through the build DAG to build a calibration corpus.  Their
scripts are 151 and 193 lines, and **about fifteen lines of each are about the design**: the parameter
axes, the DAG factory, the platform name.  Everything else is the same code written twice.

| concern | `vecmult_sweep.py` | `fir_block_sweep.py` |
|---|---|---|
| build a `BuildConfig`, with or without a platform | yes | yes |
| run the DAG `through=...`, `force=True` | yes | yes |
| turn an exception into a record instead of stopping | yes | yes |
| collect failed steps into an error string | yes | yes |
| time each point | yes | yes |
| harvest `results/resources.json` (top / module_sum / integration) | yes | yes |
| save the summary **after every point** | yes | yes |
| `--resume` by skipping points already recorded `ok` | yes | yes |
| `--dry-run` stopping at `codegen_dut` | yes | yes |
| progress printing and an exit code | yes | yes |

Neither copy is wrong.  The problem is that each was *learned* separately, and the lessons are written
into one file at a time.  `vecmult_sweep.py`'s save docstring records one of them:

> writing only at the end means an interruption at point 15 saves nothing *and* leaves the previous
> run's file in place, which is worse than saving nothing — a stale corpus that reads as a fresh one.

That is a framework-level insight sitting in an example.  The third sweep will either rediscover it or
not, and "not" is silent.

**The user-facing consequence** is that `docs/examples/vecmult/sweep.md` cannot honestly tell a reader
how to write a sweep.  Documenting 150 lines of boilerplate as the reference teaches copy-paste; the
page currently carries a warning saying so and pointing here.

### Precedent

This is the same gap `run_dag_cli` closed one level down.  Examples were hand-rolling per-example
`main()`s to drive a `BuildDag`; `waveflow/build/cli.py` now owns the introspection CLI and each
example passes a factory.  A sweep runner is that, for the loop above it — and should look like it:

```python
run_dag_cli(dag_factory, *, description, default_through, root_dir, extra_args, params_from_args)
```

## Not GridSearchCV

The obvious analogy is `sklearn.model_selection.GridSearchCV`, and it is the wrong one.  `GridSearchCV`
is an **optimizer**: it cross-validates, scores, and returns `best_params_`.  A synthesis sweep is a
**census** — it measures every point to build a corpus, and there is no score to maximize and no
held-out fold at sweep time.  Borrowing the name would have people looking for `scoring=` and
`best_estimator_`.

The right analogue is `ParameterGrid` — the iterator — plus a runner that owns what sklearn has no
concept of, because sklearn's estimators fit in milliseconds and these points cost ~45 seconds of
Vitis each:

* **resume**, because a sweep gets interrupted;
* **incremental persistence**, for the reason quoted above;
* **per-point failure isolation** — "a point that blows up is data, not a stop";
* **cost accounting**, so "K syntheses covered N design points" is auditable from the library.

## Design sketch

Three pieces, mirroring the `run_dag_cli` split of *what to run* / *how to run it* / *how to invoke it*.

### `ParamGrid` — the axes

```python
grid = ParamGrid(dwid=(32, 64, 128, 256), vlen=(512, 1024, 4096, 16384))
len(grid)            # 16
list(grid)           # [{"dwid": 32, "vlen": 512}, ...] — outer-to-inner in declaration order
grid.label(point)    # "dwid32_vlen512"
```

Cartesian product, deterministic order, one dict per point.  A `label` derived from the point rather
than hand-written per example, so the summary key and the progress line cannot disagree.

Two things it must support because the existing sweeps need them:

* **a derived axis** — `fir_block`'s `--realization serial|unroll|both` maps a presentation name onto
  `unroll_lane ∈ (False,) / (True,) / (False, True)`.  Either `ParamGrid` takes an axis whose CLI
  spelling differs from its parameter spelling, or that mapping stays in the example.  See open
  decision 2.
* **subsetting from the CLI** — `--ntap 8 16` restricts one axis without touching the others.

### What the summary is for {#what-the-summary-is-for}

Today `results/sweep.json` carries `top`, `module_sum` and `integration` per point.  Once
`plans/integration_record.md` lands, **all three are in the record store** — and two copies of the
same numbers, with the untracked one easy to leave stale, is precisely the failure this tier exists to
prevent.

Nothing reads them: a grep for `sweep.json` finds only prose and the unrelated `sim_sweep.json` of the
VMAC timing example.  So the cost of dropping the blobs is zero, and the summary becomes a genuine
**run log**:

```json
{"point": {"dwid": 64, "vlen": 4096}, "ok": true, "elapsed": 44.8,
 "filed": ["vec_mult-ea9406fe"]}
```

What was attempted, what failed, how long it took, and *pointers* to what it filed.  The log says what
happened; the store says what was measured.  Neither can go stale against the other because they
answer different questions.

**Failures stay here rather than moving to the corpus.**  A corpus row is a measurement, and a point
that failed to synthesize produced none — its counters would be absent and its `measured_at`
meaningless.  Encoding that as a null row makes every reader forever, `fit()` included, responsible
for knowing that some rows are not data, to record something only the sweep cares about.

That is also why **resume cannot read the store alone**: a point whose records are filed is done, but
a point that *failed* is invisible there, so a store-only resume would retry every failure on every
run.

### `SweepRunner` — execution and persistence

```python
runner = SweepRunner(
    dag_factory=build_vecmult_dag,
    root_dir=HERE,
    platform="zynq7020_vecmult_sweep",          # work tier; None for a dry run
    platforms_root=HERE / "calib" / "work",
    part="xc7z020clg484-1", clk_freq=100e6,
    summary=HERE / "results" / "sweep.json",
)
result = runner.run(grid, through="resources", resume=True)
```

Owns exactly the ten rows of the table above.  Per point it returns
`{point, ok, elapsed, error?, filed?}` — the log shape from
[What the summary is for](#what-the-summary-is-for), not today's counter blobs.

That is a **breaking change to `results/sweep.json`**, made deliberately and cheaply: the file is
untracked, regenerated by any re-run, and nothing reads it.  The alternative — carrying `top` /
`module_sum` / `integration` alongside a store that now holds all three — is the duplication this
tier exists to prevent.

**A failing point never stops the sweep.**  It is recorded with its error and the run continues, which
is the behaviour both scripts chose independently: a point that fails to synthesize is information
about the design space, and losing the other fifteen to it is the wrong trade.

### `sweep_cli` — the entry point

```python
if __name__ == "__main__":
    raise SystemExit(sweep_cli(runner, grid, description="Sweep vecmult resource points."))
```

Supplies `--dry-run`, `--resume`, `--out`, and one `--<axis>` per grid axis, derived from the grid
rather than declared.  Today `fir_block` has per-axis flags and `vecmult` has none — not a decision,
just which script someone extended.

### What an example is left with

```python
GRID = ParamGrid(dwid=(32, 64, 128, 256), vlen=(512, 1024, 4096, 16384))
RUNNER = SweepRunner(dag_factory=build_vecmult_dag, root_dir=HERE,
                     platform="zynq7020_vecmult_sweep", platforms_root=HERE / "calib" / "work",
                     part=PART, clk_freq=100e6)
```

Plus the docstring explaining *why those axes* — which is the part worth reading and the part no
framework can supply.

## Phases

### P0 — the equivalence gate (after the summary format settles)

Both sweeps produce a `results/sweep.json`.  Capture the current one for each as a golden, and assert
the refactored runner reproduces it **byte-identically** for a `--dry-run` over the full grid.

Dry-run because it needs no Vitis, exercises `points()` / `label()` / config construction / record
shape / save / resume, and runs in seconds.  The synthesis path is covered by P4.

{: .warning }
Capture the golden **after** the summary sheds its number blobs, not before.  A golden taken now pins
a format that `plans/integration_record.md` makes redundant, and the first thing this plan would do is
break its own gate.

**Gate:** a dry-run sweep of each example produces the committed golden summary unchanged.

### P1 — `ParamGrid`

Product, order, `label`, subsetting.  Pure and fast, so it gets ordinary unit tests: order is
declaration order, a single-value axis still yields dicts, an empty axis yields nothing rather than
silently dropping the axis.

**Gate:** `list(ParamGrid(**axes))` equals each example's current `points()` output, element for
element, including order.

### P2 — `SweepRunner`

Execution, record shape, incremental save, resume, cost accounting.  The lessons from the existing
docstrings move here **with their reasoning**, because a comment explaining why a save is incremental
is worth more in the framework than in one example.

**Gate:** P0 golden for both examples; plus tests that a raising point is recorded rather than
propagated, that resume skips only `ok` points, and that an interrupted run leaves a summary marked
incomplete.

### P3 — `sweep_cli`, and the examples collapse onto it

Rewrite both sweep scripts against the three pieces.  Expected: ~150 → ~25 lines each.

**Gate:** P0 golden still byte-identical; both scripts' CLI surface is a superset of what they have
today (nothing an existing invocation could do is lost).

### P4 — re-measure once, on the real path

Run one example's real sweep end to end and confirm the records land in the work tier exactly as
before.  `vecmult` is the cheap one: 16 points, ~12 minutes, and the new
`test_the_committed_grid_agrees_with_the_record_store` already asserts the outcome.

**Gate:** `-m vitis`-free test suite at baseline, plus the grid/store agreement test green after a
real re-sweep.

### P5 — docs

`docs/examples/vecmult/sweep.md` loses its "this is a template, not a pattern" warning and gains a
short **Writing your own sweep** section against the real API.  A guide page under
`guide/resource_model/` on building a corpus is the likely home for the general version.

**Gate:** docs guard; the symbol guard already refuses a page naming an API that does not exist.

## Open decisions

1. **Where does it live?**  It drives a `BuildDag`, which argues `waveflow/build/sweep.py` beside
   `cli.py`.  Its *purpose* is calibration data, which argues `waveflow/calib/`.  Leaning `build/`:
   the module's dependencies are the DAG and `BuildConfig`, and `waveflow/calib` importing the build
   layer would be a new direction of dependency.

2. **Derived axes.**  `--realization serial|unroll|both → unroll_lane` is a presentation mapping over a
   boolean axis.  Options: (a) `ParamGrid` takes an optional CLI spelling per axis; (b) the example
   maps CLI to axes before constructing the grid.  (b) keeps `ParamGrid` a plain product and puts the
   naming where the vocabulary is.  Leaning (b), but it means `sweep_cli` cannot derive *every* flag
   from the grid.

3. ~~**Does resume still need a summary file?**~~  **RESOLVED: yes, and the summary becomes a pure
   log.**  Failures are not in the store — a failed point produced no measurement — so a store-only
   resume would retry every failure forever.  The summary keeps resume state and sheds the numbers;
   see [What the summary is for](#what-the-summary-is-for).

4. **Parallelism.**  16 points took 716 s serially and the machine was mostly idle.  Vitis runs in
   separate work directories could go 4-wide.  Explicitly **out of scope for v1**: this tree has
   already been bitten by two concurrent Vitis runs colliding in one build directory, and the fix is
   per-point isolated work dirs, which is its own change.  Worth a note in the class docstring so the
   next person knows it was considered rather than missed.

5. **Should the summary keep `grid`?**  Both write the axes into the JSON.  Useful for reading a
   summary standalone; redundant with the points themselves.  Keep it: a log that cannot say what
   space it covered is harder to interpret than one that repeats itself, and unlike the counter blobs
   the axes are not stored anywhere else.

## Risks

* **The golden is a dry run.**  It cannot catch a change in how a *real* point harvests
  `resources.json` or files records.  P4 exists for that, and is the only phase needing Vitis.
* **Two examples is a thin basis for an abstraction.**  Mitigated by the fact that both were written
  independently and converged on the same ten concerns — that is evidence the shape is real rather
  than a generalization from one case.  Where they genuinely differ (per-axis CLI flags, `--out`),
  the union is taken rather than a compromise invented.
* **`fir_block`'s sweep is the more featureful one** and is the one at risk of losing capability in the
  collapse.  P3's gate is explicitly a superset check, not a "still works" check.
