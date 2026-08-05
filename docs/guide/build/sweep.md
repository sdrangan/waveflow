---
title: Sweeping a design
parent: Build System
nav_order: 8
audience: python
api: [ParamGrid, Stage, SweepRunner, SweepResult, sweep_cli]
summary: "Running a design at every point of a parameter grid and keeping what the steps measured. ParamGrid is the points, in declaration order; Stage is one pass through the DAG, so a timing sweep can run RTL and pysim at different cadences; SweepRunner owns resume, incremental save and per-point failure isolation; sweep_cli is the entry point. An example supplies its axes, its DAG factory and its platform — the parts that are about the design."
---

# Sweeping a design

A calibration corpus is built by **sweeping**: elaborate a design at each point of a parameter grid,
run it through the [build DAG](./corecomp.md), and let the steps file what they measured.

```python
GRID   = ParamGrid(vlen=(512, 1024, 4096, 16384), dwid=(32, 64, 128, 256))
RUNNER = SweepRunner(dag_factory=build_vecmult_dag, root_dir=HERE,
                     platform="zynq7020_vecmult_sweep", platforms_root=HERE / "calib" / "work",
                     part="xc7z020clg484-1", clk_freq=100e6)

def main(argv=None):
    return sweep_cli(RUNNER, GRID, description="Sweep vecmult resource points",
                     stages=[Stage("resources")],
                     dry_run_stages=[Stage("codegen_dut", use_platform=False)], argv=argv)
```

That is a whole sweep script. What it does *not* contain — resume, incremental save, per-point
failure isolation, progress, the exit code — is the point: those are properties of sweeping, not of
this design, and they live here.

{: .note }
> **Why not `GridSearchCV`?** The resemblance is real but the shape is wrong. `GridSearchCV` is an
> *optimizer*: it cross-validates, scores, and returns `best_params_`. A synthesis sweep is a
> **census** — it measures every point to build a corpus, with no score to maximize and no held-out
> fold at sweep time. The closer analogue is `ParameterGrid`, plus a runner owning what sklearn has no
> concept of because its estimators fit in milliseconds while these points cost ~45 seconds of Vitis.

## `ParamGrid` — the points

A Cartesian product over named axes. **Declaration order is iteration order**, so a grid reads the way
it runs:

```python
ParamGrid(vlen=(512, 1024), dwid=(32, 64))
# vlen is the OUTER loop:
#   {vlen: 512, dwid: 32}, {vlen: 512, dwid: 64}, {vlen: 1024, dwid: 32}, ...
```

Order is worth choosing rather than accepting. `vecmult` puts `vlen` outside because its grid is
organised along the [BRAM regimes](../../examples/vecmult/sweep.md), so a partial run covers whole
regimes rather than a slice of each.

**A single-value axis is a constant.** `samp_i=(2,)` rides into every point without branching — how a
design says "this one is held fixed" without a second concept for it. Constants stay out of labels,
where they could only pad every log line.

### Build axes versus workload axes

```python
ParamGrid(dwid=(32, 64), nwords=(128, 512), _workload=("nwords",))
```

A **build** axis is a `HwParam`: changing it produces different hardware, so everything from
elaboration onward re-runs. A **workload** axis is a runtime input — the hardware is unchanged and
only the simulation repeats.

This is not bookkeeping. Utilization does not depend on workload at all —
[`ResourceModel.get_params`](../calib/model.md) drops `**runtime` for exactly that reason — while a
timing corpus is mostly workload points against one build. A sweep that could not tell them apart
would re-synthesize for a change in `nwords`.

## `Stage` — one pass through the DAG

A resource sweep is one stage per point: synthesize, attribute, file. A **timing** sweep is two, and
deliberately at different cadences:

```python
runner.run(grid, stages=[
    Stage("pysim_collect"),                                  # cheap: whole grid, every edit
    Stage("rtl_collect", when=lambda p: p["dwid"] == 32),    # expensive: a subset
])
```

[`TimingModel`](../timing_model/component_residual.md) keeps its `rtl/` and `pysim/` trees apart
because RTL is Vitis-expensive and pysim is every-edit-cheap, joining them at fit time on the feature
point. Two useful things fall out of stages rather than needing modes of their own:

- **a stage can skip a point** (`when`) — that is the RTL-subset case;
- **resume is per `(point, stage)`**, so re-running after a change to the cheap side does not re-run
  the expensive one.

`use_platform=False` is for a dry run: nothing was synthesized, so there is no report to file, and
attaching a platform would only invite a half-written library.

## `SweepRunner` — what it owns

| concern | why it is not in the example |
|---|---|
| build a `BuildConfig` per point, with or without a platform | identical everywhere |
| run the DAG `through` the stage, `force=True` | ditto |
| **a failing point is recorded, not raised** | losing fifteen good points to one bad one is the wrong trade |
| **the summary is written after every point** | see the warning below |
| `--resume` skipping `(point, stage)` pairs already `ok` | hours of synthesis should not be lost to one crash |
| progress and the exit code | so a sweep is usable in a script |

{: .warning }
> **Incremental save is not tidiness.** Writing only at the end means an interruption at point 15
> saves nothing **and** leaves the previous run's file in place — a stale summary that reads as a
> fresh one. That lesson was learned once, in one example's docstring, and the other two sweeps did
> not have it. It lives here now.

### A failing point is data

```text
[7/16] vlen4096_dwid128 resources ...
    FAILED  csynth: II not met
[8/16] vlen4096_dwid256 resources ...
    ok  54.1s  filed 1
```

The sweep continues and the failure is recorded with its error. A point that fails to synthesize is
information about the design space; a sweep that quietly covered 19 of 24 points and reported 24 would
put a hole in the fitted region exactly where an agent would later be told it was interpolating.

## The summary is a log, not a corpus

```json
{"point": {"vlen": 512, "dwid": 64},
 "stages": {"resources": {"ok": true, "elapsed": 54.1, "filed": ["vec_mult-e934dd1a"]}}}
```

What was attempted, what failed, how long it took, and **pointers** to what was filed. The numbers
themselves live in the [record store](../calib/corpus.md#it-is-derived-never-authoritative); carrying
them here too would make a second copy, with the untracked one easy to leave stale.

That also settles where **failures** belong. A corpus row is a *measurement*, and a point that failed
to synthesize produced none — encoding it as a null row would make every reader forever, `fit()`
included, responsible for knowing that some rows are not data. So failures stay in the log, which is
also why `--resume` reads the summary rather than the store: a failed point is invisible in a store,
and a store-only resume would retry it on every run.

## `sweep_cli` — the entry point

The sibling of [`run_dag_cli`](./corecomp.md), one level up. It supplies `--dry-run`, `--resume`,
`--out`, and **one `--<axis>` flag per numeric or string axis, derived from the grid**:

```bash
python -m examples.vecmult.vecmult_sweep --dry-run          # codegen only, no toolchain
python -m examples.vecmult.vecmult_sweep --vlen 512 --resume
python -m examples.fir_block.fir_block_sweep --ntap 8 16 --realization unroll
```

Boolean axes get no automatic flag, because `--unroll-lane 0 1` is a worse interface than a named
choice. An example supplies that itself:

```python
_REALIZATIONS = {"serial": (False,), "unroll": (True,), "both": (False, True)}

sweep_cli(..., extra_args=[(("--realization",), {"choices": tuple(_REALIZATIONS), ...})],
          grid_from_args=lambda g, a: g.subset(unroll_lane=_REALIZATIONS[a.realization]))
```

`serial` and `unroll` are *this design's* vocabulary for what the flag selects, so the mapping belongs
with the design rather than in the framework.

{: .note }
> **A label must not change when the grid is narrowed.** `--vlen 512` leaves that axis with one value,
> which makes it *look* like a constant — and a label recomputed from the narrowed grid would drop it.
> `--vlen 512 --resume` would then look for `dwid64` in a summary written as `vlen512_dwid64`, match
> nothing, and silently re-run every point it already had. `ParamGrid` carries its label axes through
> `subset()` for this reason; it was a real bug, found by running the CLI rather than by reasoning
> about it.

## Two tiers: sweep, then publish

A sweep writes to an **untracked work tier** and a deliberate act promotes it:

```bash
waveflow_calib publish examples/vecmult/calib/work/zynq7020_vecmult_sweep \
                       examples/vecmult/calib/platforms/zynq7020_vecmult --apply
```

A sweep churns and re-runs freely; a library is reviewed. Naming a tracked library directly would let
`Platform.resolve` find it and write into it, which only `publish` may do — see the
[work → publish flow](../platform/workflow.md).

{: .warning }
> **Setting a platform at all is what makes a sweep produce records.** Without one,
> `InspectSynthStep` attributes the report and has nowhere shared to file it, so the measurements
> survive only as numbers a human copies into source. That is not hypothetical: it is exactly how
> `examples/vecmult`'s corpus began life.

## See also

- [Core Components](./corecomp.md) — the `BuildDag` and `BuildConfig` a sweep drives.
- [The corpus](../calib/corpus.md) — what the filed records become, and why it is derived rather than
  maintained.
- [The sweep](../../examples/vecmult/sweep.md) — a real 16-point grid and what it was designed to
  separate.
