---
title: The calibration workflow
parent: Calibration
nav_order: 5
audience: python
api: [BusCalib, StreamTimingModel, Platform, CalibBusStep, CollectTimingStep, FitTimingStep]
summary: "Calibration storage is two-tier: sweeps write a churny untracked calib/work/<name>/; the publish_calib command promotes the stable artifacts (platform.json, mm_bus.json, points/, component params + corpus -- never the raw firing trees) into the tracked calib/platforms/<name>/. publish is dry-run by default, a byte-compare no-op on unchanged files, and refuses a coverage regression (a thinner re-fit) unless forced. The reference zynq7020_bfm_100mhz platform is built end-to-end by examples/mem_copy/calibrate_platform.py and reproduces the writer RTL period to 0.0%."
---

# The calibration workflow

Calibration parameters are **infra-wide**: once a shared component is calibrated, every project reuses
it. That makes two things matter — a stray run must never clobber shared parameters, and a
re-run that produces the *same* fit must not churn git. Both fall out of a **two-tier** storage split.

## Two tiers: work vs. tracked

```
calib/work/<name>/        untracked (gitignored).  Sweeps, tests, and the DAG calib steps write here
                          freely — it churns.
        │  publish_calib
        ▼
calib/platforms/<name>/   tracked (committed).  The shared parameters. EXACTLY ONE writer: publish.
```

A sweep points the [`BusCalib`](./bus_model.md) / [`StreamTimingModel`](./component_residual.md) fits at
the **work** dir. When you are satisfied, one command promotes the result into the **tracked** library.
Because the tracked dir has a single writer, a test can't reach it; because a re-fit on the same corpus
is deterministic, an unchanged promotion writes nothing.

## `publish_calib`

```bash
publish_calib calib/work/<name> calib/platforms/<name>            # dry-run: print the plan, write nothing
publish_calib calib/work/<name> calib/platforms/<name> --apply    # write only the changed files
```

- **Dry-run by default.** It prints a plan — `+ created`, `~ updated`, `= unchanged` — and writes
  nothing. `--apply` performs it.
- **No-op when unchanged.** Each artifact is byte-compared against the tracked copy; identical files are
  never rewritten, so a deterministic re-publish produces **0 files written** and no git diff.
- **Only the stable artifacts** are promoted; the churny raw firing trees stay behind:

  | Published | Left in the work dir |
  |---|---|
  | `platform.json` (identity) | `components/<c>/rtl/` |
  | `mm_bus.json` + `points/*.json` | `components/<c>/pysim/` |
  | `components/<c>/params.json` + `corpus.csv` | |

- **Coverage-regression guard.** publish refuses (exit 1) to replace a fit with one built from *fewer*
  datapoints — bus point files, or a component's `corpus.csv` rows — so a thin re-sweep can't silently
  clobber a richer library. `--force` overrides it.

Under the hood `build_plan(work, tracked)` computes the plan (pure inspection) and `apply_plan(plan,
force=…)` writes the changed files; the CLI is a thin wrapper.

### Why publish is not a DAG step

Promotion to shared infra is a deliberate "I'm satisfied" act, not a build side effect. The DAG steps
([`CalibBusStep`](./bus_model.md#automating-it-calibbusstep),
[`CollectTimingStep` / `FitTimingStep`](./component_residual.md#automating-it-collecttimingstep--fittimingstep))
populate the **work** dir; `publish_calib` is the manual gate to the tracked one.

## The `.gitignore` rules

Two rules encode the split — `/calib/work/` is ignored, and the tracked library is re-included past the
global `*.json` ignore:

```gitignore
/calib/work/                    # the churny work tier — never committed
!/calib/platforms/**/*.json     # ...but the tracked library IS committed (params + identity)
```

## The reference platform, end to end

`calib/platforms/zynq7020_bfm_100mhz/` is built reproducibly by
[`examples/mem_copy/calibrate_platform.py`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/calibrate_platform.py):

1. **Seed the identity** — `Platform.resolve` writes `platform.json` (`xc7z020clg484-1`, 100 MHz).
2. **Fit the bus law** — `add_run` the measured burst laws (write `nwords + 2·(num_trans−1)`, read
   `nwords + (num_trans−1)`) at two sizes, then `fit()` → `mm_bus.json`.
3. **Fit the writer residual** — `collect_rtl` the measured RTL spans (183 cyc at n=128, 615 at n=512)
   and `collect_pysim` a run **with the bus law active**, so the residual is control-only (~22 cyc),
   then `fit()` → `components/mem_w_stream_framed_done_task/`.
4. **Publish** — `publish_calib calib/work/zynq7020_bfm_100mhz calib/platforms/zynq7020_bfm_100mhz
   --apply`.

The pysim runs live (no toolchain); only the RTL spans are measured constants (gated for real by the
`-m xsi` run that produced 0.0% error). Loading the committed platform reproduces the writer RTL period
to **0.0%** at both sizes — a test pins it, so the committed params can't silently drift.

## A known limitation: collinear sizes

The reference platform was swept at two sizes whose `num_trans` and `nwords` are proportional
(`num_trans = nwords / 16`), so the two features are collinear: the fit **reproduces the measured
points exactly** but its split between a per-word and a per-burst slope is underdetermined (the writer's
residual reads as 23 at n=128, 0 at n=512). A third, non-collinear sweep size is the follow-up before
trusting the model to *extrapolate* beyond the swept range.

## See also

- [Platforms](./platform.md) — the identity the workflow seeds and confirms.
- [The bus-transfer model](./bus_model.md) / [Component residuals](./component_residual.md) — the two
  fits the sweep produces.
- [Build system](../build/) — the DAG the calibration steps plug into.
