---
title: Fitting the timing model
parent: Interleaver (gather)
nav_order: 11
---
# Fitting the timing model

[The timing model](./timing_model.md) has two parameters, `latency` and `ii`. This page **recovers them
from the RTL** — the [direct method](../../guide/calib/fit.md): run the kernel at a range of sizes, record
`(n, cycles)`, and fit a line. The result ships to the platform library so any build loads it.

This is the half of the calibration story `mem_copy` has none of, because the gather is the design's own
kernel. Read it as a recipe for fitting **your** custom stage — each step says whether it is a built-in
routine or code you write.

## Step 1 — measure the per-firing span from the RTL trace

`cycles` is a **measured** number, read off a **traced** RTL run. A traced run leaves two files the
measurement reads (both produced by the ordinary trace rung — see
[Tracing a kernel run](../../guide/timing/trace_steps.md)):

- **`vcd_path`** — `<top>_trace.vcd`, the **waveform**: every RTL net's value on every cycle, dumped by the
  XSI run with tracing turned on (the `RtlSimStep`).
- **`manifest`** — `results/trace_manifest.json`, a **map from the Python graph** (each component, channel,
  and port) **to the mangled RTL net names** the waveform carries. Synthesized net names are
  version-specific, so this map — emitted by `TraceManifestStep` from
  `composite_top_spec(comp).trace_manifest()` — is what lets "the writer's done port" resolve to an actual
  net in the VCD.

The built-in reader binds the two and hands back one span per firing:

```python
from waveflow.utils.trace import load_trace

bt = load_trace(manifest, vcd_path)      # BoundTrace: the manifest resolved against the waveform
firings = bt.component_firings(inst)      # one Firing per job, anchored on ap_done
spans = [f.span for f in firings]         # first-input → ap_done, per firing
```

**You rarely run this by hand.** The built-in `ExtractBurstsStep` runs it **after each traced RTL sim** and
writes a per-firing table to `results/timing_events.json`; the residual calibration steps
(`CollectTimingStep` / `FitTimingStep`) read that table and fit a model — the whole loop is a DAG. So **if
your stage's cost is its `ap_done` firing span, this path is already automated**: you attach a model and the
steps measure and fit it for you (this is how the reusable mem-streams the interleaver sits on were
calibrated — see [component residuals](../../guide/calib/component_residual.md)). `component_firings()`
anchors on `ap_done`, not the last output beat, because a posted `m_axi` store keeps the component working
after its final beat.

### Why the gather takes a different route

The gather's cost is **not** its firing span. `component_firings()` measures first-input → `ap_done`, and
`il_compute`'s inputs are its [stream-of-blocks](../../guide/concurrency/python/sob.md); in this
reader-bound pipeline the stage spends most of each firing *waiting* for `il_load` to hand it those blocks,
so the firing span is dominated by that wait, not the gather. The window that is the gather's **own** time
is where the loop is actually writing output — its output block's **write-enable**.

That net is not in the manifest — the manifest names channels and ports, not a component's internal
block-RAM nets — so there is no built-in for it, and you read it out of the raw VCD. **Finding it is not
guesswork.** A VCD declares every net up front in `$var` lines, so list the ones under your component and
the output write-enable stands out:

```console
$ grep '\$var' interleaver_inband_trace.vcd | grep il_compute
$var wire  1 ... il_compute_inband_task_64_256_U0_y_blk_we0 $end   # output block write-enable  <-- this
$var wire 32 ... il_compute_inband_task_64_256_U0_y_blk_d0  $end   # the data it writes
$var wire  1 ... il_compute_inband_task_64_256_U0_ap_done   $end   # firing boundary
...
```

(or open the VCD in a waveform viewer and read off `il_compute`'s output-block port). `y_blk` is the
compute's output block and `_we0` its write-enable — high on exactly the cycles the gather writes an
element. That one net is all
[`measure_compute_spans.py`](../../../examples/interleaver/measure_compute_spans.py) specializes; the rest
is generic VCD parsing. So the two things you change for **your** stage are the signal it keys on and the
no-stall gate:

```python
# measure_compute_spans.py — the two things you change for your own stage
_WE_RE = re.compile(r"il_compute_inband_task_\d+_\d+_U0_y_blk_we0$")   # your stage's active-work net

def measure_compute_spans(vcd_path, sizes):
    ...
    # a firing is CLEAN iff its we0 is a SINGLE contiguous window between ap_done marks
    if len(firing_wins) == 1:              # one burst, no gap → no output backpressure
        s, e = firing_wins[0]
        spans.setdefault(n, []).append(e - s)   # the loop's own cycle count
```

The gate is what makes the number trustworthy: input backpressure only delays *when* the burst starts (not
its length), and a dip inside the burst — output backpressure — is rejected. What survives is the loop's
own cycles. (These are the write-enable / posted-write cautions from
[trace pitfalls](../../guide/timing/trace_pitfalls.md), respected — the gather writes on-chip block RAM,
not a posted store, so its write window *is* its work window.)

Unlike the automated `ExtractBurstsStep` path, this custom measurement is a **script you run** — it does not
record to `timing_events.json`; it returns the `(n, cycles)` numbers, which you wire into the fit below.

> The framework packages this same signal-discovery + span extraction as
> [`add_sob_signals` / `extract_sob_span`](../../guide/timing/sob.md) on the `VcdParser` — you name the
> component and block and it finds the write-enable and returns the contiguous windows (and they render on a
> timing diagram). `measure_compute_spans.py` predates that API and parses the VCD directly; the two do the
> same thing.

## Step 2 — run the sweep

A line needs several sizes. Build the RTL **once** at the block capacity `N`, then drive several runtime
sizes `n ≤ N` in one XSI run — the design reads `n` from the descriptor, so a single synthesis covers the
whole sweep. [`run_and_measure`](../../../examples/interleaver/measure_compute_spans.py) does this
end-to-end:

```python
from measure_compute_spans import run_and_measure

measured = run_and_measure(sizes=(128, 128, 256, 256, 512, 512), n_max=512)
# {128: 128.0, 256: 256.0, 512: 512.0}   — every firing a clean, contiguous n-cycle burst
```

This one script drives the whole sweep — there is no separate DAG step to invoke. Internally it *is* the
ordinary build DAG: `generate_inband` + `generate_tb` emit the DUT and testbench, the built-in
`XsiHarnessStep` assembles the harness, `run.bat` runs `csynth → xsim` with a trace (the same rungs as the
[RTL-sim page](../memcpy/rtlsim.md)), and Step 1's parser reads the spans. It needs the toolchain (Vitis
HLS + Vivado `xsim`); the parser itself is pure and testable on a saved VCD.

## Step 3 — fit the line

The fit is one built-in call — a [`LinCalibModel`](../../guide/calib/models.md) over the `(n, cycles)`
rows. [`fit_compute_model`](../../../examples/interleaver/calibrate_compute.py) wraps it:

```python
def fit_compute_model(n_to_cycles, out_dir):
    df = pd.DataFrame([{"n": n, "cycles": c} for n, c in sorted(n_to_cycles.items())])
    model = LinCalibModel(basis=["n"], target="cycles", fit_intercept=True,
                          coeff_names=["n"], path=Path(out_dir) / "params.json")
    model.fit(df)                       # → {"n": 1.0, "intercept": 0.0}, i.e. cycles = n
    return model.save_model()
```

Two points suffice in principle (two unknowns); the three here confirm the line. The coefficient on `n` is
`ii`, the intercept is `latency − ii` — recovered here as `ii = 1`, `latency = 1`.

## Step 4 — ship it to the platform

Store the fit where a build finds it. [`calibrate`](../../../examples/interleaver/calibrate_compute.py)
writes it into the [platform library](../../guide/calib/platform.md) under
`components/il_compute_task/params.json`, keyed like any component:

```python
from calibrate_compute import calibrate
calibrate("waveflow/calib/platforms", "zynq7020_bfm_100mhz")   # → components/il_compute_task/params.json
```

A build selecting the platform then loads it with no re-fit (`InterleaverInband(compute_calib_dir=…)`).
Unlike the shipped bus and mem-stream models, this fit is the design's *own* — same place, but the
interleaver's contribution, not reusable infra.

## Running it

The measured points are already wired into `calibrate_compute.N_TO_CYCLES`, so the two commands are:

```bash
# (re)measure from RTL — needs Vitis HLS + Vivado xsim; prints N_TO_CYCLES to paste back
python examples/interleaver/measure_compute_spans.py

# fit the measured points and ship into the platform library
python examples/interleaver/calibrate_compute.py --work-root waveflow/calib/platforms
```

(The default `--work-root calib/work` writes a local, untracked fit for development instead.)

## Does it work?

Loaded, the pysim charges the real cost: the gather's per-firing span lands on the RTL burst it was measured
from.

```python
il = run_interleaver(nj=3, n=256, compute_calib_dir=fit_dir)
il.gather.job_span_cyc        # [256.0, 256.0, 256.0] — the contiguous burst XSI saw
```

## Fitting your own stage — the checklist

1. **Declare** the model on the stage — [the model page](./timing_model.md). *(your code, ~5 lines)*
2. **Pick the span.** `bt.component_firings(inst)` → `Firing.span` if the firing span is your cost —
   *built-in*. For a tighter window, a small VCD parser keyed on your net — *custom; copy
   `measure_compute_spans.py` and change the signal + gate.*
3. **Sweep** several sizes in one build with the build-once/run-many `run_and_measure` pattern. *(reuses
   built-in build steps; your factory supplies the `generate_*` calls.)*
4. **Fit** with `LinCalibModel.fit`. *(built-in.)*
5. **Ship** with a `calibrate()` into the platform library. *(a few lines around the built-in
   `Platform.resolve`.)*

## See also

- [Fitting a timing model](../../guide/calib/fit.md) — the direct method, in general.
- [The timing model](./timing_model.md) — what `latency` and `ii` mean (the previous page).
- [Platforms](../../guide/calib/platform.md) — the library the fit ships into.
- [Component residuals](../../guide/calib/component_residual.md) — the *other*, DAG-driven method, for the
  reusable mem-streams the interleaver inherits.
- [Trace pitfalls](../../guide/timing/trace_pitfalls.md) — the span-measurement cautions Step 1 respects.
