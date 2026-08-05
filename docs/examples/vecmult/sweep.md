---
title: The sweep
parent: Vector Multiply (resource modelling)
nav_order: 4
audience: python
api: [ParamGrid, SweepRunner, Stage, sweep_cli]
summary: "Driving 16 design points through the build DAG to get an attributed resource report per point. How the sweep script is written — a ParamGrid, a SweepRunner, one Stage and sweep_cli — and what the grid was designed to separate: the two BRAM regimes that look like different laws and are one ceiling. Also the committed corpus, and why it is Python source rather than the sweep's JSON."
---

# The sweep

One synthesis gives you one point. A resource *model* needs a grid, and which grid you choose decides
what you are able to learn — a sweep that only samples one regime will validate a law it never tested.

## How the sweep is written

The whole of `vecmult_sweep.py`, minus its docstring, is four declarations and a `main`:

```python
GRID = ParamGrid(vlen=(512, 1024, 4096, 16384), dwid=(32, 64, 128, 256))

RUNNER = SweepRunner(dag_factory=build_vecmult_dag, root_dir=HERE,
                     platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                     part=PART, clk_freq=CLK_FREQ,
                     extra_params={"live_output": False})

STAGES         = [Stage("resources")]
DRY_RUN_STAGES = [Stage("codegen_dut", use_platform=False)]

def main(argv=None):
    return sweep_cli(RUNNER, GRID, description="Sweep vecmult resource points",
                     stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv)
```

Read in order, each line says one thing about *this* design:

| declaration | what it settles |
|---|---|
| `ParamGrid` | the points — 4 × 4 = 16, `vlen` first so it is the **outer** loop |
| `SweepRunner` | which DAG to run, and the device and work-tier library the measurements are keyed by and filed into |
| `Stage("resources")` | run that DAG through its `resources` step: synthesize, attribute, file |
| `DRY_RUN_STAGES` | what `--dry-run` does instead — stop at codegen, and attach no platform |
| `sweep_cli` | turn the three of them into a program with `--help` |

Everything a sweep needs beyond those is supplied: resume, a summary written after every point, a
failing point recorded rather than aborting the run, progress output and an exit code. None of it is
written here, and none of it can be forgotten. See
[Sweeping a design](../../guide/build/sweep.md) for the API in full.

{: .note }
> **`force=True` is why each point is honest — and you do not pass it.** Without it the DAG would see
> up-to-date artifacts from the *previous* point and skip, and the report on disk would then be
> attributed to the wrong configuration. `SweepRunner` forces every run for exactly that reason.

## Running it

`sweep_cli` derives one flag per axis from the grid, so the two axes above are also the CLI:

```bash
python -m examples.vecmult.vecmult_sweep --dry-run   # codegen only, no Vitis — a cheap pre-flight
python -m examples.vecmult.vecmult_sweep             # all 16 points, ~15 minutes
python -m examples.vecmult.vecmult_sweep --vlen 512 --resume     # one row, continuing a stopped run
```

Each point re-runs the whole DAG with different `params`, so every measurement comes from a design
that was **re-generated, re-simulated, re-checked against its golden and re-synthesized** at that
configuration. Nothing is reused across points except the source.

At a quarter of an hour the grid is easily long enough to be interrupted, which is why the summary is
written after every point rather than at the end, and why `--resume` skips the `(point, stage)` pairs
already recorded as ok — [both belong to the runner](../../guide/build/sweep.md#what-you-get-for-free)
rather than to this script.

## The grid, and what it was designed to separate

```python
DWIDS = (32, 64, 128, 256)      # LW = 2, 4, 8, 16
VLENS = (512, 1024, 4096, 16384)
```

That is not a uniform box for the sake of coverage. Each `vlen` row was chosen to put the buffer in a
different regime:

| `vlen` | bank depth at LW=2 … LW=16 | what it exercises |
|---|---|---|
| 512 | 256 … 32 | banks far shallower than a block — and the LUTRAM corner at the end |
| 1024 | 512 … 64 | every bank rounds up to exactly one block |
| 4096 | 2048 … 256 | straddles the knee |
| 16384 | 8192 … 1024 | banks deeper than a block |

## The results

| vlen | LW=2 | LW=4 | LW=8 | LW=16 |
|---|---|---|---|---|
| **512** | 2 | 4 | 8 | **0** |
| **1024** | 2 | 4 | 8 | 16 |
| **4096** | 4 | 4 | 8 | 16 |
| **16384** | 16 | 16 | 16 | 16 |

*(BRAM18 blocks. DSP is `LW` at every one of the 16 points; LUT and FF are on the
[model page](./resource_model.md).)*

## The two BRAM regimes

Look at the table twice. The **16384** row is flat at 16 regardless of lane count. The **512** and
**1024** rows track `LW` exactly. Those look like two different laws, and they are one:

```text
BRAM18 = LW × ceil( (vlen / LW) / entries_per_block )
```

Each of the `LW` banks is `vlen/LW` deep, and **a bank shallower than one block still occupies a whole
one**. That single ceiling produces both behaviours:

- **Partition-bound** — bank shallower than a block, so the ceiling is 1 and `BRAM = LW`. The 512 and
  1024 rows: same data, 2 → 4 → 8 → 16 blocks. Partitioning costs everything.
- **Data-bound** — bank deeper than a block, so `LW` cancels against the ceiling and BRAM depends only
  on total size. The 16384 row: 16 at every lane count. Partitioning is free.

{: .warning }
> **Why the grid has to span both.** Drop the ceiling and `LW` cancels unconditionally, leaving
> `vlen / entries` — right on the 16384 row and wrong by up to **4×** on the others. The opposite
> simplification, `BRAM = LW`, fits 11 of these 16 points and looks convincing until the data-bound
> column shows up. **Neither error is visible from a grid that samples one regime.** Choosing the
> grid is part of choosing the model.

`entries_per_block` is 1024 for 16-bit elements, not `18432/16 = 1152`, because a block has legal port
shapes rather than a bit budget — see [`bram_estimate`](./resource_model.md#what-you-supply-and-what-the-library-supplies).

## The corner

`vlen=512, dwid=256` gives banks 32 deep and **BRAM = 0**: HLS declined block RAM and used LUTRAM
instead, with the storage reappearing in fabric. That is a *discontinuity*, not a bad measurement, and
it is [predicted by the device rule](./resource_model.md#the-corner-predicted-not-absorbed) rather
than treated as an outlier.

Five extra syntheses pinned where it happens — bank depths 40, 48, 56, 60 and 63 are all LUTRAM, 64 is
block RAM — so the threshold sits between 1008 and 1024 bits per bank.

## Where the measurements go

Each point's report is attributed and **filed as a record**, keyed by the module's elaborated
structure, into an untracked work tier:

```text
examples/vecmult/calib/work/zynq7020_vecmult_sweep/modules/<key>/resource/records.jsonl
```

A deliberate publish promotes that into the example's tracked library, which is what the model reads:

```bash
waveflow_calib publish examples/vecmult/calib/work/zynq7020_vecmult_sweep \
                       examples/vecmult/calib/platforms/zynq7020_vecmult --apply
```

Two tiers because a sweep is exploratory and re-runs freely, while a library is reviewed: the
[work → publish flow](../../guide/platform/workflow.md) keeps an interrupted or experimental run from
quietly editing committed measurements. The sweep also writes `results/sweep.json`, a human-readable
summary; it is untracked, and nothing reads it.

The records are the [raw tier](../../guide/calib/corpus.md#it-is-derived-never-authoritative) — the
corpus the fit reads is *derived* from them on demand, never stored twice.

{: .note }
> **`GRID` in `vecmult_corpus.py` is not that corpus.** It is the same 16 measurements committed as
> Python source, and it now serves two narrower jobs: the **oracle** the tests check predictions
> against with no toolchain installed, and the fallback corpus for `add_rm(None)` when there is no
> platform to read a store from. Keeping a second independent copy is a feature while one checks the
> other — it stops being one the moment both claim to be the source.

## Next

- [Resource models](./resource_model.md) — turning these 16 points into two rules, one fit and one
  integration term.
- [Sweeping a design](../../guide/build/sweep.md) — `ParamGrid`, `SweepRunner` and `sweep_cli` in
  full, for when you write one of your own.
