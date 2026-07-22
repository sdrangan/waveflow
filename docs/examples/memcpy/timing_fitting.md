---
title: Fitting the models
parent: Memory Copy
nav_order: 9
---

# Fitting the models

The [two models](./timing_model.md) ship pre-fit for supported platforms, so `mem_copy` needs no
calibration of its own. This page shows how those parameters are **produced** — the sweep, the fit, and
where the result is stored so *other* accelerators on the same platform reuse it. `mem_copy` is in fact
the design the reference `zynq7020_bfm_100mhz` platform was fit from.

## The sweep

Both models are fit from a **sweep** — the design run at a few input sizes, each yielding measured
`(size, cycles)` points. `mem_copy` is swept at two sizes, `n_words ∈ {128, 512}` (a 4× range):

- from each size's **RTL trace**, the per-transfer bus span off the `m_axi` ports (the input to the bus
  fit) and the writer's firing span (the input to the residual fit);
- from a **pysim run at the same size** with the bus model already active, the writer's firing span
  pysim currently predicts.

The reproducible builder is [`examples/mem_copy/calibrate_platform.py`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/calibrate_platform.py):
it fits the bus law, then runs the writer's pysim so its residual is *control-only* (the bus term already
charged), and writes everything into a work directory.

## The fit

**The bus law.** The `m_axi` span is regressed on `(num_trans, n_words)` — a line whose coefficients are
the per-burst and per-word costs. On this platform the write channel fits `n_words + 2·(num_trans − 1)`
and the read channel `n_words + (num_trans − 1)`, landing in `mm_bus.json`:

```json
"write": { "num_trans": 0.070, "nwords": 1.121, "intercept": -2.0 }
```

**The writer residual.** The component fit joins the RTL span against the (bus-charged) pysim span and
fits the gap. The two swept points make the corpus:

| `n_words` | `num_trans` | RTL span | pysim span (bus on) | residual |
|---|---|---|---|---|
| 128 | 8 | 183 | 160 | **23** |
| 512 | 32 | 615 | 615 | **0** |

The residual is what pysim *misses* once the bus term is charged — small (≤23), because pysim already
models the fill and drain. Loading the fitted models back, the pysim reproduces the RTL period to
**0.0%** at both sizes (a test pins this on the committed platform).

> **A known limitation.** The two swept sizes have `num_trans = n_words / 16`, so the two features are
> collinear: the fit reproduces the measured points exactly but its split between a per-word and a
> per-burst slope is underdetermined (the residual reads 23 → 0). A third, non-collinear size would let
> it *extrapolate*, not just interpolate — the outstanding follow-up.

## Is it a build step?

Both halves are **DAG steps**, so a sweep can run as part of a build:

- [`CalibBusStep`](../../guide/calib/bus_model.md) reads a run's trace, measures each `m_axi` bundle's
  per-transfer span, and refits `mm_bus.json`.
- [`CollectTimingStep` / `FitTimingStep`](../../guide/calib/component_residual.md) collect a run's RTL +
  pysim firings into a component's corpus and fit the residual.

`calibrate_platform.py` wires these together into the reproducible builder above. One honest note: its
RTL spans (183 / 615) are **measured constants** — captured from the real `-m xsi` run that closed the
loop — while the pysim side runs live; a full toolchain sweep would re-measure them each time.

## Where it is stored — and reused

Promotion to shared infra is deliberate, so calibration is **two-tier**. A sweep writes an untracked
work directory; the [`publish_calib`](../../guide/calib/workflow.md) command promotes the stable
artifacts into the git-tracked platform library:

```
calib/platforms/zynq7020_bfm_100mhz/
    platform.json                                       # {part, clk_freq_hz}
    mm_bus.json  +  points/                              # the bus model + its corpus
    components/mem_w_stream_framed_done_task/            # the writer's residual
        params.json  +  corpus.csv
```

Because the library is keyed by an [FPGA-part identity](../../guide/calib/platform.md) and the mem-streams
are framework components, **any** accelerator that composes `MemRStream` / `MemWStream` on this platform
loads these same parameters — no re-fit. That is the whole point of fitting `mem_copy` once: the numbers
it produced are the platform's, not the example's.

## See also

- [Timing models](./timing_model.md) — the models this page fits.
- [Timing model fitting](../../guide/calib/) — the general system: the two-level split, the DAG steps,
  the platform library, and `publish_calib`.
- [The calibration workflow](../../guide/calib/workflow.md) — the two-tier work → publish flow in full.
