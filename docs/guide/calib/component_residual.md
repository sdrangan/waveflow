---
title: Component residuals
parent: Timing model fitting
nav_order: 5
audience: python
api: [TimingModel, StreamTimingModel, CollectTimingStep, FitTimingStep]
summary: "A component's control residual is the delay pysim is MISSING once the bus term is charged: residual = rtl_span - pysim_span + current_dly, fit per (component, platform). TimingModel collects RTL firings (from a trace) and pysim firings (from a run) into independent trees, joins them on the feature point (nwords, num_trans), and fits a CalibModel; predict() returns the delay a FreeRunComp injects via timed_delay. Stored shared (platform_dir -> the committed library) or custom (calib_dir -> a project dir). CollectTimingStep / FitTimingStep automate it."
---
# Component residuals

The second level of the [split](./index.md#two-levels-what-is-a-platform-property-what-is-a-component-property):
once the [bus term](./bus_model.md) is charged, what remains is the component's own **control cost** —
the per-firing overhead the loosely-timed pysim does not already account for. A `TimingModel` fits that
remainder and a `FreeRunComp` injects it.

## The residual is what pysim *misses*

Crucially, the fitted quantity is not the component's *total* control cost — it is the **residual**
between what the RTL did and what pysim already predicts:

```
residual = rtl_span − pysim_span + current_dly        # all in cycles
```

The `+ current_dly` corrects for whatever delay the model *already* injected on that run, so the fit is
self-correcting: any run is a datapoint (no model-disabled baseline needed), and online calibration
converges from a zero seed. Because pysim already models the fill and the overlapped drain, the residual
is small — on the reference platform the writer's is ~22 cycles, even though its *total* control cost
measured in RTL is ~41; pysim was already accounting for the rest.

## Where the model lives

A calibrated component **carries its own** `StreamTimingModel` — `MemWStream.__post_init__` creates it
and calls `add_timing_model`, so during a run the component *predicts* its delay through that attached
instance (via `timed_delay`) and accumulates `firing_records` on it. But the corpus and fitted params
live **on disk**, keyed by `calib_dir` — the object itself is almost stateless.

That means collection and fitting can be done by *either* the component's attached instance — what the
[DAG steps](#automating-it-collecttimingstep--fittimingstep) do, reaching it via
`discover_timing_models(design)` — *or* a **separate** `StreamTimingModel` pointed at the same
`calib_dir`. The directory is the coordination point: the component-side instance predicts, a
calibration-side instance fits, and the two never need to be the same object. (This is how
`calibrate_platform.py` works — the run attaches one model to the writer, and the driver holds a second
at the same dir to collect and fit.)

## Collect, join, fit

RTL and pysim are collected **independently** — different cadence (RTL is synthesis-expensive, pysim is
every-edit-cheap) — into `rtl/` and `pysim/` trees, then joined at fit time on the **feature point**
(`nwords`, `num_trans`), not the run id. Here from a standalone instance (the DAG steps do the same
calls on the component's attached one):

```python
from waveflow.calib.timing_model import StreamTimingModel
from waveflow.hw.clock import Clock

tm = StreamTimingModel(component="mem_w_stream_framed_done_task",
                       calib_dir="…/components/mem_w_stream_framed_done_task", clk=Clock(freq=100e6))

for nw in (128, 512):
    tm.collect_rtl(events, run_id=f"n{nw}")                 # one row per firing, from ExtractBurstsStep
    tm.collect_pysim(writer.firing_records, run_id=f"n{nw}")  # writer = the calibrated component, run with the bus law active
tm.fit()                                                    # -> corpus.csv + params.json
```

- **`collect_rtl`** filters an [`ExtractBurstsStep`](../timing/trace_steps.md) events dict to this
  component's firings. Only *uncontended* firings are calibratable — `is_record_valid` (default
  `blocked == 0`) drops any firing that stalled on a full downstream channel, because that measured
  contention, not the component's own cost.
- **`collect_pysim`** records the firings a pysim run populated (via `timed_delay`, below). Spans arrive
  in time and are divided by `clk.period` to cycles, so the stored corpus is all-cycles and
  clock-independent.
- **`gen_data_frame`** joins them into `corpus.csv` — `features… | span_rtl | span_pysim | current_dly | residual` — and records `coverage` (`matched` / `rtl_only` / `pysim_only` feature points), so a thin
  fit is a *visible* gap, not a silent one.
- **`fit`** fits a `CalibModel` residual and writes `params.json`; it **raises** if nothing joined, so a
  no-op can't leave a seed model looking fitted. `predict(row)` returns the additional delay (length
  `num_targets`, default 1), clamped at 0. `reset(corpus=…, params=…)` wipes the trees / the fit to
  recalibrate from scratch.

## Recording the pysim firings: `timed_delay`

Where does the `writer.firing_records` list `collect_pysim` reads come from? A calibrated `FreeRunComp`
does not call `predict` directly — it calls **`timed_delay`** in its `run_iter`, which both **predicts**
the delay *and* **records** the firing (its features + the delay it just predicted) onto
`self.firing_records`:

```python
dly = self.timed_delay({"nwords": nw, "num_trans": math.ceil(nw / 16)})  # predict + record
if dly:
    yield self.timeout(dly)
```

That record is exactly one row of the pysim corpus. The loop is: attach the model, run the sim so
`timed_delay` populates `firing_records`, then `collect_pysim(comp.firing_records, run_id)` writes them
into the `pysim/` tree. Crucially the run needs **no fitted model** to collect — an unfitted model
predicts `0.0` and records nothing extra, so the firing is captured with `current_dly = 0`; that is the
`+ current_dly` self-correction above, letting *any* run be a datapoint.

`MemRStream` / `MemWStream` both carry this `timed_delay` hook (the writer's models the posted-write
drain, the reader's its own control cost), inert until a model is fit. It is the recording counterpart
of the plain `predict` shown in [Adding a timing model](../timing_model/insertion.md): same prediction,
plus the firing record the residual fit needs.

## Two kinds of component: where the residual lives

The fit is identical; only the storage scope differs (the
[two kinds](./index.md#two-kinds-of-component-shared-infra-vs-custom)):

|                        | Shared-infra component                   | Custom component                      |
| ---------------------- | ---------------------------------------- | ------------------------------------- |
| e.g.                   | `MemRStream`, `MemWStream`           | your accelerator's kernel             |
| stored in              | the committed**platform library**  | a**project-local dir you pick** |
| selected by            | `platform_dir` (keyed by task-body id) | an explicit`calib_dir`              |
| reused across projects | yes — no recalibration                  | no — specific to your design         |

Both mem-streams take both knobs; `_resolve_calib_dir` gives `calib_dir` precedence (the project-local
override) and otherwise resolves `platform_dir` into `<platform>/components/<task-body>/`. Neither set
→ uncalibrated.

## Automating it: `CollectTimingStep` / `FitTimingStep`

In a build DAG these two steps drive collection and fitting from a **design factory** — a callable
returning the built (and, for collect, pysim-run) component tree — so they are generic and never mention
a particular design:

```
ExtractBurstsStep -> CollectTimingStep   (collect_rtl + collect_pysim for every attached model)
                  -> FitTimingStep        (fit each from its corpus; skip, not fail, the ones that don't yet join)
```

`CollectTimingStep` appends one run's RTL + pysim firings to every attached model's corpus;
`FitTimingStep` fits each from the accumulated corpus, **skipping with a report** any model whose corpus
does not yet join (a sweep in progress has partial coverage — forcing a fit there would only raise).
`discover_timing_models` walks the component tree to find which models to collect for.

## See also

- [Adding a timing model to a component](../timing_model/insertion.md) — the usage side: attaching the
  model and charging its delay, which this page fits.
- [The bus-transfer model](./bus_model.md) — the level this one assumes is already charged.
- [Platforms](./platform.md) — where a shared component's residual is keyed and stored.
- [The calibration workflow](./workflow.md) — collecting a sweep and publishing the result.
- [Tracing a kernel run](../timing/trace_steps.md) — the `ExtractBurstsStep` firing table `collect_rtl`
  reads, including the `blocked` column and the ap_done window.
