---
title: Managing a platform
parent: Platforms
nav_order: 4
audience: python
api: [BusCalib, StreamTimingModel, Platform, CalibBusStep, CollectTimingStep, FitTimingStep]
summary: "Calibration storage is two-tier: sweeps write a churny untracked calib/work/<name>/; the publish_calib command promotes the stable artifacts (platform.json, mm_bus.json, points/, component params + corpus -- never the raw firing trees) into the shipped, in-package library waveflow/calib/platforms/<name>/. publish is dry-run by default, a byte-compare no-op on unchanged files, and refuses a coverage regression (a thinner re-fit) unless forced. The reference zynq7020_bfm_100mhz platform is built end-to-end by examples/mem_copy/calibrate_platform.py and reproduces the writer RTL period to 0.0%."
---

# Managing a platform

Three things you do to a platform: **create** one, **look inside** one, and **publish** into one.

```bash
waveflow_calib new     calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
waveflow_calib show    calib/platforms/myboard
waveflow_calib publish calib/work/myboard calib/platforms/myboard --apply
```

## Creating one

```bash
waveflow_calib new calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
```

seeds the directory and its [identity manifest](./identity.md).  A platform is also created
*implicitly* the first time a build selects a name that does not resolve — convenient, but the explicit
command is what you want when setting up deliberately, because it is the one place to declare a
non-default counter vocabulary:

```bash
waveflow_calib new calib/platforms/asic45 --part tsmc45 --clk 1e9                    --res-types cell_area macros regs
```

That is the seam a non-FPGA technology enters through — see
[`res_types`](./identity.md).  Omit it and the platform is measured in the FPGA counters, which is not
written to the manifest at all, so an ordinary Vitis platform's manifest stays exactly as it was.

## Looking inside one

```bash
waveflow_calib show waveflow/calib/platforms/zynq7020_bfm_100mhz
```

```text
platform 'zynq7020_bfm_100mhz'
  part xc7z020clg484-1   clock 100.0 MHz
  measured in: lut ff dsp bram uram srl

bus law      : fitted   (2 corpus points)

timing residuals (2):
  mem_r_stream_framed_task                   fitted      3 corpus row(s)
  mem_w_stream_framed_done_task              fitted      2 corpus row(s)

module records (35 configuration(s)):
  FirCmdRx                 5 config(s)   hls_estimate=29 record(s)
  FirCompute              26 config(s)   hls_estimate=29 record(s)
  MemRStream               2 config(s)   hls_estimate=29 record(s)
  MemWStream               2 config(s)   hls_estimate=29 record(s)

synthesis time these records represent: 25.8 min
```

Worth reading that last line: it is the cost the library saves you, recorded from the runs that
actually happened rather than estimated.  ``-v`` lists every module configuration and its counters.

{: .note }
> The two timing residuals above are keyed by **function name alone** — they predate the
> configuration-qualified key, so they are not known to describe any particular memory width.  A model
> that loads one reports that in its confidence rather than implying a match.  See
> [Directory layout](./layout.md).

## Publishing into one

Calibration parameters are **infra-wide**: once a shared component is calibrated, every project reuses
it. That makes two things matter — a stray run must never clobber shared parameters, and a
re-run that produces the *same* fit must not churn git. Both fall out of a **two-tier** storage split.

## Two tiers: work vs. tracked

```
calib/work/<name>/                  untracked (gitignored).  Sweeps, tests, and the DAG calib steps
                                    write here freely — it churns.
        │  publish_calib
        ▼
waveflow/calib/platforms/<name>/    tracked (committed), and shipped as package data so a
                                    pip-installed user resolves it. EXACTLY ONE writer: publish.
```

A sweep points the [`BusCalib`](../calib/bus_model.md) / [`StreamTimingModel`](../calib/component_residual.md) fits at
the **work** dir. When you are satisfied, one command promotes the result into the **tracked** library —
`waveflow/calib/platforms/` for the shipped reference platforms, or a user/project overlay for a
platform you calibrate locally (see [Platforms](./identity.md)). Because the tracked dir has a single
writer, a test can't reach it; because a re-fit on the same corpus is deterministic, an unchanged
promotion writes nothing.

### The command

```bash
publish_calib calib/work/<name> waveflow/calib/platforms/<name>            # dry-run: print the plan, write nothing
publish_calib calib/work/<name> waveflow/calib/platforms/<name> --apply    # write only the changed files
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
([`CalibBusStep`](../calib/bus_model.md#automating-it-calibbusstep),
[`CollectTimingStep` / `FitTimingStep`](../calib/component_residual.md#automating-it-collecttimingstep--fittimingstep))
populate the **work** dir; `publish_calib` is the manual gate to the tracked one.

## The `.gitignore` rules

Three rules encode the split. A blanket `*.json` ignore covers build artifacts, so **both** tracked
libraries have to be re-included past it, and the work tier is ignored on its own:

```gitignore
/calib/work/                       # the churny work tier — never committed
!waveflow/calib/platforms/**       # the shipped reference library IS committed
!calib/platforms/**                # ...and so is a PROJECT's own library
```

{: .warning }
> That last line is easy to omit and expensive to miss. `platforms_root` defaults to
> `calib/platforms/`, so without it a project publishing exactly as documented would write into a
> directory git silently drops — the publish appears to succeed and nothing is committed. If you set
> up a library in a new repository, check `git status` sees it.

## The reference platform, end to end

`waveflow/calib/platforms/zynq7020_bfm_100mhz/` is built reproducibly by
[`examples/mem_copy/calibrate_platform.py`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/calibrate_platform.py):

1. **Seed the identity** — `Platform.resolve` writes `platform.json` (`xc7z020clg484-1`, 100 MHz).
2. **Fit the bus law** — `add_run` the measured burst laws (write `nwords + 2·(num_trans−1)`, read
   `nwords + (num_trans−1)`) at two sizes, then `fit()` → `mm_bus.json`.
3. **Fit the writer residual** — `collect_rtl` the measured RTL spans (183 cyc at n=128, 615 at n=512)
   and `collect_pysim` a run **with the bus law active**, so the residual is control-only (~22 cyc),
   then `fit()` → `components/mem_w_stream_framed_done_task/`.
4. **Publish** — `publish_calib calib/work/zynq7020_bfm_100mhz
   waveflow/calib/platforms/zynq7020_bfm_100mhz --apply`.

The pysim runs live (no toolchain); only the RTL spans are measured constants (gated for real by the
`-m xsi` run that produced 0.0% error). Loading the committed platform reproduces the writer RTL period
to **0.0%** at both sizes — a test pins it, so the committed params can't silently drift.

## A known limitation: collinear sizes

The reference platform was swept at two sizes whose `num_trans` and `nwords` are proportional
(`num_trans = nwords / 16`), so the two features are collinear: the fit **reproduces the measured
points exactly** but its split between a per-word and a per-burst slope is underdetermined (the writer's
residual reads as 23 at n=128, 0 at n=512). More sizes do **not** fix this if they are all multiples of
16 — `num_trans` stays `nwords / 16` and the collinearity persists. What breaks it is a size where
`ceil(nwords / 16)` *departs* from `nwords / 16` — a **small or non-multiple-of-16 transfer** (e.g.
`nwords = 17` → 2 bursts, `nwords = 16` → 1) that changes the burst-to-word ratio. Adding those to the
grid identifies the per-word (1) and per-burst (2 write / 1 read) costs cleanly; it is the follow-up
before trusting the model to *extrapolate* beyond the swept range.

## See also

- [Platforms](./identity.md) — the identity the workflow seeds and confirms.
- [The bus-transfer model](../calib/bus_model.md) / [Component residuals](../calib/component_residual.md) — the two
  fits the sweep produces.
- [Build system](../build/) — the DAG the calibration steps plug into.
