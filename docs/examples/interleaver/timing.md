---
title: Visualizing timing
parent: Interleaver (gather)
nav_order: 9
---
# Visualizing timing

The loosely-timed [pysim](./timing_model.md) and the RTL **agree** on the steady-state period — about
**300 cycles per job** in pysim against **302** measured on the RTL, ≈0.7%. That agreement rests on every
stage's timing being a *loaded model*: the platform's [bus law](../../guide/calib/bus_model.md) and
[mem-stream residuals](../../guide/calib/memstream.md) (shared infra the interleaver inherits), **plus the
interleaver's own fitted compute model** (the half it calibrates itself). This page is about *seeing* that
timing — what each side measures, how the six stages overlap, and where the period comes from. The
[next page](./timing_fit.md) shows how the compute parameters were derived.

## What the RTL measures

An XSI trace gives cycle-accurate per-firing timing — every stage's `ap_done`, beat by beat. Reading each
stage's firing cadence off it:

| stage | firings/job | period |
|-------|:-----------:|:------:|
| `cmd_rx` | 1 | 302 |
| **`MemRStream`** (reader) | **2** | **151 each** |
| `il_load` | 1 | 302 |
| `il_compute` (gather) | 1 | 302 |
| `il_store` | 1 | 302 |
| `MemWStream` (writer) | 1 | 302 |

Every stage fires once per job at 302 cycles — **except the reader, which fires twice** (P then X, 151
cycles each). So the pipeline is **reader-bound at 302 cyc/job**: moving both the index vector `P` and the
source `X` over one `m_axi` bus is the critical path, and everything else fits inside it.

## What the pysim measures

The pysim never runs the RTL. It charges **modeled** delays and lets backpressure emerge from the bounded
FIFOs between stages:

- the **bus law** — each `m_axi` transfer's cost, `nwords + (num_trans − 1)` cycles per read
  ([`BusCalib`](../../guide/calib/bus_model.md));
- the **mem-stream residuals** — the reader's ≈15-cycle and writer's ≈22-cycle own control cost, on top of
  the bus term ([the mem-stream residual](../../guide/calib/memstream.md));
- the **compute loop model** — the gather's `cycles = n` ([the timing model](./timing_model.md)).

Each free-running stage records a per-firing `fire_log`. No toolchain runs — it is the fast, functional
model — and it lands at **300 cyc/job**.

## The pipeline at a glance

![The six-stage pipeline activity across six jobs](./images/pipeline_activity.svg)

Every stage on one cycle axis, six jobs, rendered straight from the pysim `fire_log`s. Reading it top to
bottom is reading the dataflow — one job descends the stack:

- **`cmd_rx`** receives the `InterleaverCmd` and frames **two** reads (P then X) for the reader.
- **`MemRStream` (gmem0)** — the **two bands per job** are those two reads; it is busy almost continuously,
  and it is the **bottleneck** (moving `P` and `X` over one bus).
- **`il_load`** lands `P` and `X` into the on-chip stream-of-blocks.
- **`il_compute`** runs the gather `Y[i] = X[P[i]]` — its band has visible **slack** (256 of the ≈300-cycle
  job), the one stage this design calibrates itself, *not* on the critical path.
- **`il_store`** frames the writer's stream `[MemWCmd | Y]`.
- **`MemWStream` (gmem1)** bursts `Y` to memory and echoes the done.

**The bands are occupancy, not work.** A band is a stage's firing window — from when it starts a firing to
when it commits — so for a free-running stage it includes time spent **stalled on backpressure**, not just
its own compute. `cmd_rx` is the clearest case: it does about **5 cycles** of real work (build two
commands), but its steady-state band fills the whole ≈300-cycle job — because it cannot dispatch the next
job's commands until `MemRStream` has taken this one. The long bar is *throttling*, not effort. The reader's
own two bands, by contrast, are close to solid work: it is the bottleneck, so nothing downstream throttles
it. (The thin seam between consecutive bands is only a legibility device, so you can count the jobs.)

> **How to regenerate it.** The figure is an `InterleaverFiguresStep`, run through the standard build DAG
> CLI:
>
> ```bash
> python examples/interleaver/interleaver_figures.py            # --through figures (the default)
> python examples/interleaver/interleaver_figures.py --force    # re-render even if up-to-date
> ```
>
> Unlike [`mem_copy`'s figures](../memcpy/timing.md) — which consume an RTL *trace* the DAG produced
> upstream — this one renders straight from the pysim timeline, so it is a **leaf** step (no toolchain, no
> upstream artifact). The SVG is deterministic, so a re-run is a no-op unless the timeline moved and the git
> diff is the review signal. Inside, each stage's `fire_log` (its per-firing `(start, end)` windows) becomes
> one [`ActivityDiagram`](../../guide/timing/activity.md) lane:
>
> ```python
> lanes = []
> for stage, label, colour in stages:
>     # each firing window -> a run of active cycles (a small seam trimmed off the end, for legibility)
>     runs = [np.arange(round(s), round(e) - seam) for s, e in stage.fire_log]
>     lanes.append((label, np.concatenate(runs), colour))
>
> ad = ActivityDiagram(lanes, time_unit="cycle")
> fig, _ax, _ = ad.plot(mode="band", ...)              # activity bands, not per-transition value boxes
> ```

## The agreement — and what it took

RTL 302 vs pysim 300, ≈0.7%. It holds because every stage's cost is now a loaded model. But getting there
took calibrating the **reader** — and that is the interesting part. `mem_copy` is *writer*-bound, so it
only ever needed the writer's residual; the reader's was never fit. The interleaver is the first
**reader-bound** design, and it ran **≈10% under** the RTL until a reader residual was fit
([the mem-stream residual](../../guide/calib/memstream.md) — the fixture the interleaver forced into
existence). The lesson: **you calibrate the stage a design actually bottlenecks on**; an un-fit residual
only shows where it lands on the critical path.

The compute stage's *own* per-firing span, meanwhile, matches **exactly** — 256 pysim == 256 RTL. It is not
the bottleneck, so it does not set the period, but its cost is faithful: a variant that made the gather the
critical path would already have the right model.

## Next: where the compute parameters came from

The compute model's `cycles = n` did not come from nowhere. [Fitting the timing model](./timing_fit.md)
shows the measure → fit → ship recipe — reading `il_compute`'s per-firing span off a full-pipeline XSI run,
fitting the loop law, and storing it in the platform library. That is the custom-component half of the
calibration story, the one this example exists to teach.

## See also

- [The timing model](./timing_model.md) / [Fitting the timing model](./timing_fit.md) — the compute model
  and how its parameters are fit.
- [The mem-stream residual](../../guide/calib/memstream.md) — the reader/writer infra residuals the period
  rests on, and the fixture that fits them.
- [Activity Diagrams](../../guide/timing/activity.md) — the renderer behind the figure.
- [`mem_copy` — Visualizing timing](../memcpy/timing.md) — the writer-bound sibling that reproduces its RTL
  period to 0.0%.
