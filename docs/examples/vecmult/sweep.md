---
title: The sweep
parent: Vector Multiply (resource modelling)
nav_order: 4
audience: python
summary: "Driving 16 design points through the build DAG to get an attributed resource report per point, and what the grid was designed to separate: the two BRAM regimes that look like different laws and are one ceiling. Also the committed corpus, and why it is Python source rather than the sweep's JSON."
---

# The sweep

One synthesis gives you one point. A resource *model* needs a grid, and which grid you choose decides
what you are able to learn — a sweep that only samples one regime will validate a law it never tested.

## Running it

```bash
python -m examples.vecmult.vecmult_sweep --dry-run   # codegen only, no Vitis — a cheap pre-flight
python -m examples.vecmult.vecmult_sweep             # 16 points, ~8 minutes
```

Each point re-runs the whole DAG with different `params`, so every measurement comes from a design
that was **re-generated, re-simulated, re-checked against its golden and re-synthesized** at that
configuration. Nothing is reused across points except the source.

```python
config = BuildConfig(root_dir=HERE, params={**p, "live_output": False})
results = build_vecmult_dag().run(config, through="resources", force=True)
```

`force=True` matters. Without it the DAG would see up-to-date artifacts from the *previous* point and
skip — and the report on disk would then be attributed to the wrong configuration.

{: .warning }
> **The summary is written after every point, not at the end.** A 16-point grid is ~8 minutes, easily
> long enough to be interrupted, and writing only at the end means an interruption at point 15 saves
> nothing *and* leaves the previous run's file in place — a stale corpus that reads as a fresh one.
> The file records `"complete": true/false` so a partial one cannot be mistaken for a whole one, and
> `--resume` skips points already recorded as ok.

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

## Writing your own sweep

{: .warning }
> `vecmult_sweep.py` is ~150 lines, and only about fifteen of them are about *this design* — the
> parameter axes, the DAG, the platform name. The rest (resume, incremental save, per-point failure
> isolation, the argparse entry point) is the same code `fir_block_sweep.py` also contains.
>
> That duplication is a gap in the framework rather than something to copy. A `SweepRunner` that owns
> it is planned; until it lands, take `vecmult_sweep.py` as a working template rather than as a
> pattern worth reproducing by hand, and expect this section to shrink to a few lines.

## Next

- [Resource models](./resource_model.md) — turning these 16 points into two rules, one fit and one
  integration term.
