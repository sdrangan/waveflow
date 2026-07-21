# TimingModel — per-component timing calibration on top of CalibModel

**Status: design sketch.** The measurement half is shipped (PRs #111/#112: trace steps,
per-firing timing table, `ap_done` windows, `blocked` column). This plan is the *fitting* half —
the orchestration that turns those measurements into a per-component delay the pysim `run_iter` can
apply. See `plans/memcpy_timing_calibration.md` for the measured law this has to reproduce.

## The gap this fills

`waveflow/calib/calib.py` already has the model machinery and says so in its own docstring —
"it doesn't have any infrastructure for collecting data":

- `CalibModel` / `LinCalibModel` — `fit` / `predict` / `score`, the state_dict artifact I/O, the
  seed fallback (`load_or_default`). `LinCalibModel(fit_intercept=False, transform=…)` **is** the
  mem_copy law shape: through-origin physical rates, with `clk_period` folded into the features so
  the fit is clock-independent.
- `CalibDataFrame` — the append-only datapoint corpus (csv, timestamps).

What is missing is the glue **between a traced run and those models**: pull this component's rows
from the timing table, decide which are valid, join RTL vs pysim into a residual, fit, and expose a
`predict` the component calls. That glue is `TimingModel`. It **composes** `CalibModel`, it does not
replace or subclass it.

## What the session proved (constraints the design must honour)

1. **Fit the RESIDUAL, not the absolute span.** `predict` returns the delay pysim is *missing*
   (`RTL_span − pysim_span + current_dly`, see below). Fitting absolute re-derives the `nwords` term
   pysim already models and destroys the property that makes it a model: contention *emerging* from
   the bounded channel rather than being baked in.
2. **Validity = `blocked == 0`, already computed.** `ExtractBurstsStep` emits `blocked` per firing
   (from FIFO occupancy, the only reliable backpressure signal). `get_data_from_vcd` is therefore
   "load `<top>_timing.json`, select this component, keep `blocked == 0`" — not stall-detection from
   scratch.
3. **The span window is `ap_done`, not the last output beat** — posted `m_axi` writes keep draining
   after `s_done`. Already handled by `component_firings`; the table's `span` is correct.
4. **`predict` degrades to a seed when unfitted** — a component with a `TimingModel` but no data yet
   must still simulate (optimistically). `CalibModel.load_or_default` already does this.

## `predict` returns the RESIDUAL (decided)

`predict` returns **only the additional delay pysim did not already account for**, applied as an
explicit `self.timeout` in `run_iter`:

```python
x = yield self.mem_if.read(nwords)      # pysim ALREADY charges the bus/mem read time here
y = process(x)
dly = self.tm.predict({...})            # only what pysim is still MISSING
yield self.timeout(dly)
```

This dissolves the bus-vs-component question at the API level: the target is defined *operationally*
as "RTL minus whatever pysim already computed." Whether the residual is bus or control is then a
question of **how good the rest of the pysim model is**, not an API fork — improve `BusTiming` and
the residual shrinks; leave it and the residual carries the difference. So the class is always
residual-only; the two-level split becomes "where you spend effort improving pysim," recorded per
[[project-two-level-calibration]].

### The residual must subtract the CURRENT prediction (self-correcting fit)

The subtlety that makes this converge from any starting point: the residual is **not** simply
`RTL_span − pysim_span`, because the pysim span *already includes whatever the model predicted on
that run*. Concretely — model currently predicts `dly = 5`, pysim total = 80, RTL = 100. The model
should have added 20 more, i.e. the true delay is `25`, not `20`:

```
pysim_span = pysim_intrinsic + current_dly          # what the run produced
want:  RTL_span = pysim_intrinsic + true_dly
so:    true_dly = RTL_span − pysim_intrinsic
                = RTL_span − (pysim_span − current_dly)
                = RTL_span − pysim_span + current_dly     # the fit target
```

**Therefore the pysim datapoint must record `current_dly`** — the prediction that was live during
that run — alongside its span. The fit target is `RTL_span − pysim_span + current_dly`. Two
consequences, both good:

- No special "baseline run with the model disabled" is needed. *Any* run is a valid datapoint as
  long as it recorded what the model contributed.
- Calibration is **online / iterative**: run pysim with the current model → measure → re-fit →
  converge. A seed of 0 (or the hand-fit constant) is just the first iterate.

## Multiple targets (API now, one for now)

`predict` returns a **list** of delays, length `num_targets` (default 1); `num_targets > 1` raises
`NotImplementedError` for now. This carries the API so components can eventually inject a delay
*between* internal processing stages:

```python
dlys = self.tm.predict({...})       # length num_targets
yield self.timeout(dlys[0]); yield self.proc0(...)
yield self.timeout(dlys[1]); yield self.proc1(...)
yield self.timeout(dlys[2]); yield self.proc2(...)
```

Each target is its **own** `CalibModel` (composition — the per-target rule the base already
enforces; a single vector regression over correlated targets would be wrong, which is the only thing
to avoid, *not* multi-target itself). So multi-target = N composed models, which the class supports
naturally.

**Why it's gated at 1 for now:** calibrating N per-stage delays needs per-stage *measurement* — the
`ap_done`/port boundaries only bound the whole firing, so splitting it across internal stages
requires intra-component visibility. That is exactly the deferred **Tier-2 tracing** (hierarchical
paths + `.v` scan). So `num_targets > 1` unblocks when Tier-2 lands; until then a component is one
firing, one residual.

## Deferred subtlety (flag, don't solve)

A scalar delay is not the whole story: **where** in `run_iter` it is injected matters. Leading vs
trailing placement gave the same *period* but different *fill* (first-completion latency), because a
writer's residual is a posted-write drain (trailing) while a reader's is control setup (leading). So
the model is really `(delay, placement)`; `placement` affects latency-accuracy even when it does not
affect throughput. Default to one placement now; carry it as a field so it is not a silent
assumption. (With `num_targets > 1` the placements are implicit in the `timeout`/`proc` interleave,
which is the more general form.)

## Storage: per-run folders, not one shared csv

A single append-only `rtl.csv` means a read-modify-write on every run, and it throws away per-run
detail — exactly what you want back when a fit is bad and you need to inspect one point (the full
burst sequence, the raw firing rows). So make **each run its own folder the source of truth**, and
let `fit()` glob them:

```
calib_dir/
  params.json                 # fitted state_dict(s) — the ONLY deploy artifact (DAG-tracked)
  runs/
    <run_id>/                 # one folder per (design, scenario) run — append-only at folder level
      rtl_firings.csv         # this component's firings from the VCD table (incl. blocked)
      pysim_firings.csv       # this component's firings incl. current_dly
      bursts.json (optional)  # full per-beat detail, for when a fit looks wrong — debug only
```

Why this beats a shared csv:

- **No RMW, concurrency-safe.** Each run writes a fresh folder; nothing edits a shared file, so
  parallel/repeated runs cannot corrupt the corpus.
- **The debug trail is free.** `bursts.json` (or whatever a subclass drops) lives beside the summary
  rows, so a bad fit is inspectable without re-running.
- **`fit()` is a glob + concat + join**, reading `runs/*/`. The two per-source frames become a
  *derived cache*, not stored state.

This layout, the discovery, and the glob/concat are **common** (base `TimingModel`) — the row schema
is the shared `ExtractBurstsStep` shape. Only the *optional extra* artifact a run drops (`bursts.json`)
might vary per subclass, and it is not on the fit path. So it is less machinery than a
concurrency-safe shared-csv RMW, not more — no per-subclass storage code needed.

`run_id` should be derived from the scenario (e.g. `n_words`, job count) so a re-run of the same
point overwrites rather than duplicates — deterministic, DAG-friendly.

## Shape

```
TimingModel  (per FreeRunComp — orchestration; COMPOSES CalibModel)
  calib_dir/                             # per-run folders (see above); params.json the deploy artifact
  models: list[CalibModel]               # length num_targets (default 1); each per-target
  features: list[str]                    # e.g. ["nwords", "num_trans"]
  num_targets: int = 1                   # predict() returns a list this long; >1 raises for now
  placement: "leading" | "trailing"      # where run_iter injects the delay (single-target case)

  # collection (the genuinely new glue)
  collect_rtl(events, run_id)            # this component's blocked==0 firings -> runs/<id>/rtl_firings.csv
  collect_pysim(component, run_id)       # this component's spans + current_dly -> runs/<id>/pysim_firings.csv
  # fit
  fit()                                  # glob runs/*/, join rtl+pysim on `features`;
                                         #   target = rtl_span - pysim_span + current_dly;
                                         #   per-target CalibModel.fit; save params.json
  # deploy
  predict(row) -> list[float]            # additional delay(s) in CYCLES; * clk.period at the call site
                                         #   load_or_default so an unfitted model returns the seed
  # lifecycle
  reset(runs=True, params=False)         # wipe the corpus (runs/) and/or the fitted params, to
                                         #   recalibrate from scratch; back to the seed
```

`reset` exists because the corpus is append-only across runs, so a stale or bad-scenario datapoint
otherwise lingers forever. `reset(runs=True)` clears `runs/` (start collecting fresh);
`reset(params=True)` deletes `params.json` (fall back to the seed on next `predict`). Default clears
runs only — the common "re-sweep from scratch" — and leaves a fitted model deployable until the next
fit replaces it.

### Component side (the user's sketch, refined)

```python
class MemWStream(FreeRunComp):
    def __post_init__(self):
        super().__post_init__()
        self.tm = StreamTimingModel(
            calib_dir=<example>/calib/mem_w_stream,
            features=["nwords", "num_trans"],
            placement="trailing",           # posted-write drain
            seed={...})                     # fixed cost until calibrated
        self.add_timing_model(self.tm)      # register on the component (tree-discoverable)

    def _run_iter_inband(self):
        ... read descriptor, buffer fwd, write data ...
        dly = self.tm.predict({"nwords": nw, "num_trans": ceil(nw/16)})
        yield self.timeout(dly * self.clk.period)   # trailing: after the write, before next firing
        ... emit response ...
```

`add_timing_model` mirrors `add_dyn_param` / `discover_dyn_params`: a walk over the built tree finds
every registered `TimingModel`, which is what lets a build step know "trace these components and,
after the RTL run, collect into their calib_dirs."

### `StreamTimingModel(TimingModel)`

Declares the per-target models a mem-stream component needs. A pure-write component (MemWStream) has
one target (`write_residual`); a component that both reads and writes could declare two. Each is a
`LinCalibModel(fit_intercept=False, transform=fold_clk)` over `features`.

## DAG integration

Extends the timing rung already on `main`:

```
... -> ExtractBurstsStep -> CollectTimingStep -> FitTimingStep
                            (per registered TM:   (per registered TM:
                             write runs/<id>/      glob runs/*, join,
                             rtl+pysim firings)    fit, save params.json)
```

- **`CollectTimingStep`** consumes `timing_events` (RTL) + the pysim run's spans; for each registered
  `TimingModel` on the design, writes a `runs/<run_id>/` folder (per above). One folder per scenario,
  so a sweep across `n_words` (once the TB's baked `h.run(3400)` + 24640-word arena are parameterized
  — the prerequisite from the calibration plan) grows the corpus without touching prior runs.
- **`FitTimingStep`** calls `tm.fit()` for each; writes `params.json`. DAG-tracked artifact, so a
  changed corpus re-fits and a design that consumes the params re-runs.

The presence of a registered `TimingModel` is the signal for both steps — a component with none is
simply not collected or fitted, and simulates from whatever its `run_iter` already does.

## Reuse ledger (what is new vs already shipped)

| piece                                      | status                                                                                                                                                                                                                                                                                  |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| fit / predict / score / state_dict / seed  | **exists** — `CalibModel` / `LinCalibModel`                                                                                                                                                                                                                                  |
| datapoint corpus (csv, timestamps)         | **exists** — `CalibDataFrame`                                                                                                                                                                                                                                                  |
| per-firing RTL table +`blocked` validity | **exists** — `ExtractBurstsStep` (#112)                                                                                                                                                                                                                                        |
| `ap_done` span windows                   | **exists** — `component_firings` (#111)                                                                                                                                                                                                                                        |
| pysim per-firing spans                     | **partial** — `transfer_spans` is a per-component convention in `mem_stream.py` (MemRStream/MemWStream each set it up), *not* on `FreeRunComp`. Lift it to the base (or have `add_timing_model` install the hook) so any calibrated component records spans uniformly. |
| `TimingModel` (compose + collect + join) | **new** — small                                                                                                                                                                                                                                                                  |
| `get_data_from_vcd` (select + filter)    | **new** — small, reads the #112 table                                                                                                                                                                                                                                            |
| `add_timing_model` / tree discovery      | **new** — mirrors `discover_dyn_params`                                                                                                                                                                                                                                        |
| `CollectTimingStep` / `FitTimingStep`  | **new**                                                                                                                                                                                                                                                                           |
| per-run folder storage + glob/concat       | **new** — small, common (base), no per-subclass code                                                                                                                                                                                                                             |
| improve `BusTiming` to shrink the residual | **later** — not a fork; residual carries whatever pysim's bus model misses until then                                                                                                                                                                                            |

## Order of work

1. `TimingModel` + `StreamTimingModel`, `num_targets=1`. `predict` returns `[dly]` from a seed;
   `collect_rtl` / `collect_pysim` (recording `current_dly`) / `fit` (target
   `rtl_span − pysim_span + current_dly`) over the per-run folders + `LinCalibModel`. Lift
   `transfer_spans` to `FreeRunComp` (or install via `add_timing_model`) so the pysim side is uniform.
2. Wire one component (MemWStream) — `add_timing_model`, the `run_iter` timeout, the two build steps.
   Gate: pysim period matches RTL to the ~1% the hand-analysis already hit, at n_words=128, from a
   0-seed after one collect+fit iteration.
3. Parameterize the TB run-bound + arena; sweep n_words; confirm the fit extrapolates (the real test
   — a single size cannot calibrate the slope).
4. Improve pysim's `BusTiming` so the residual becomes the pure per-component control cost (the
   two-level split); revisit when a second m_axi master shares a bus.
5. `num_targets > 1` — gated on Tier-2 intra-component tracing (per-stage measurement).

## Related

- `plans/memcpy_timing_calibration.md` — the measured law and the sweep result this reproduces
- [[project-memcpy-timing-instrumentation]] — the measurement arc (merged)
- [[reference-calib-statedict-artifacts]] — the state_dict + DAG-artifact + seed pattern reused here
- [[project-two-level-calibration]] — why the bus/component split matters (residual carries it until
  `BusTiming` improves)
- `waveflow/calib/calib.py` — `CalibModel`, `LinCalibModel`, `CalibDataFrame`
