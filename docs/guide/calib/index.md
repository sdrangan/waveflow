---
title: Calibration
parent: Guide
nav_order: 12
has_children: true
audience: python
api: [CalibDataFrame, CalibModel, LinCalibModel, InterpCalibModel, BusCalib, StreamTimingModel, Platform, PlatformCalib]
summary: "The waveflow.calib package: fit physically-reasonable timing models from synth/cosim measurement so the fast loosely-timed sim tracks the slow RTL. A CalibModel / CalibDataFrame layer holds the corpus and fits one target; on top of it a two-level split calibrates the m_axi bus law once per PLATFORM (BusCalib) and each reusable component's control residual per (component, platform) (StreamTimingModel), stored in a git-tracked platform library keyed by an FPGA-part identity (Platform / platform.json) and promoted from an untracked work dir by publish_calib."
---

# Calibration

A Waveflow component's timing model is **loosely-timed** (see [Timing Models](../timing_model/)): it
predicts a timeline from a few numbers — a latency, an initiation interval, a per-transfer cost. Those
numbers are properties of the *synthesized* hardware. **Calibration** is the discipline of *fitting
them from measurement* — running the kernel through synthesis or RTL cosim at a range of sizes,
recording the resulting cycle counts, and fitting a model so the fast LT simulation reproduces the slow
RTL without you transcribing report numbers by hand.

## Two levels: what is a platform property, what is a component property

The cost of a run splits into two parts that live at two different scopes:

```
        RTL cycles  =  bus transfer  +  component control
                        └─ PLATFORM ─┘   └── COMPONENT ──┘
                        (the m_axi law,   (this kernel's own
                         shared by every   overhead, per
                         accelerator)       component + platform)
```

- The **bus transfer** — how long the `m_axi` interconnect takes to move `n` words in `k` bursts — is
  a property of the **platform** (the memory system + AXI adapter), not of any one accelerator. Fit it
  **once per platform** and every accelerator on that platform reuses it. This is [`BusCalib`](./bus_model.md).
- The **component control** cost — the kernel's own per-firing overhead once the bus term is charged —
  is a property of the **`(component, platform)`** pair. This is the residual a
  [`StreamTimingModel`](./component_residual.md) fits, stored *with* the platform so the next project
  reusing the component inherits it.

Splitting them means a new accelerator loads the platform's bus law for free and fits only its own
small residual — instead of re-measuring the interconnect every time.

## Two kinds of component: shared infra vs custom

The component-control residual is fit the *same way* for two kinds of component — they differ only in
**where the fitted data lives**:

- **Shared-infra components** — the reusable framework kernels (`MemRStream` / `MemWStream`, and more
  to come). Their residual is a `(component, platform)` property, so it is stored in the **committed
  platform library** and shipped with the repo: reuse the component on a calibrated platform and you
  inherit its timing with **no re-calibration**.
- **Custom components** — your accelerator's own kernels. Their residual is specific to your design,
  not shared infra, so it is stored in a **project-local directory you choose** (typically beside the
  component).

The package serves both through one knob: a component takes a `platform_dir` (resolve into the shared
library, keyed by the component id) *or* an explicit `calib_dir` (your project-local path, which wins
if both are given). [The bus law](./bus_model.md) sits above this split — it is always platform-scoped,
because it is the shared interconnect, owned by no single component.

## One calibration layer

`BusCalib` and `StreamTimingModel` are the **same underlying fit** — a [`CalibModel`](./models.md) over
a corpus — wrapped with the collection, storage, and scoping each level needs. They differ only in what
they attach to (the shared `m_axi` interface vs. a `FreeRunComp`) and at what scope they are stored
(platform vs. component). A single timing model attachable to *any* `SimObj`, with `FreeRunComp`
specialization, is a natural future unification; today the two entry points cover the cases that exist,
and the platform-vs-component scoping is intrinsic — the bus is a shared resource — not an accident of
having two classes.

## Everything is in cycles

The fitted numbers are stored in **cycles**, not seconds, so `params.json` is clock-independent — a
re-deploy at a different simulation frequency needs no refit. The sim `Clock` is only a cycles ↔
seconds converter at the boundary. The one clock that *does* change the numbers is the **synthesis**
clock (`create_clock -period`): HLS schedules to meet it, so a different target period can change the
cycle counts. That is why a platform is keyed by part **and** clock — see [Platforms](./platform.md).

## The platform library

A calibrated platform is a directory keyed by an FPGA-part identity, holding both levels:

```
calib/platforms/<name>/
    platform.json                     # identity: {part, clk_freq_hz}   (Platform)
    mm_bus.json  +  points/            # the bus-transfer law            (BusCalib)
    components/<task-body>/            # a component's control residual  (StreamTimingModel)
        params.json  +  corpus.csv
```

Calibration data is **two-tier**: sweeps write a churny, untracked `calib/work/<name>/`; only the
`publish_calib` command promotes the stable artifacts into the tracked `calib/platforms/<name>/`. So a
stray run can never clobber shared parameters, and a deterministic re-fit produces no git diff. See
[The calibration workflow](./workflow.md).

## The pieces

The corpus and the single-target models are the primitive layer the two levels compose over: the
corpus is a [`CalibDataFrame`](./dataframe.md) — a thin `pandas.DataFrame` wrapper, one row per
measurement — and the [models](./models.md) fit one target (e.g. `cycles`) from a basis of its columns.
The [bus model](./bus_model.md) and [component residuals](./component_residual.md) build on them with
the collection, storage, and scoping that turn a raw sweep into a reusable, checked-in library.

## In this section

The platform-calibration system, then the primitive layer it composes over:

- [Adding a timing model to a component](./insertion.md) — usage-first: where a `TimingModel` plugs
  into a `FreeRunComp`, how `timed_delay` charges the delay, and why read-stalls emerge from the sim
  rather than the model.
- [Platforms](./platform.md) — the platform identity (`platform.json`, `Platform`), why a platform is
  keyed by FPGA part **and** synthesis clock, and how `BuildConfig` selects and confirms one.
- [The bus-transfer model](./bus_model.md) — `BusCalib`: the `m_axi` span law fit once per platform
  (`mm_bus.json`), measured component-independently off the ports (`measure_bus_span`).
- [Component residuals](./component_residual.md) — `StreamTimingModel`: fitting the model you attached,
  per `(component, platform)`, with the bus term already charged.
- [The calibration workflow](./workflow.md) — the two-tier `work` → `publish_calib` → `platforms`
  flow, the DAG steps that populate it, and the reference `zynq7020_bfm_100mhz` platform end to end.
- [The corpus — `CalibDataFrame`](./dataframe.md) — the primitive corpus: one timestamped row per
  measurement, a `pandas.DataFrame` under `.df`, with `save` / `load`.
- [Models](./models.md) — the primitive per-target fit / predict / score interface (`CalibModel`), the
  linear model (`LinCalibModel`), and the calibrated lookup (`InterpCalibModel`).
- [A worked example](./example.md) — the primitive layer alone: fit a `LinCalibModel` to
  `(size, cycles)`, score it, hold a point out; then a saturating curve with `InterpCalibModel`.
- [Instrumenting a calibration](./instrumentation.md) — the playbook for collecting *real* data and
  closing the LT-vs-RTL loop (worked against the FIR example).

## See also

- [Timing Analysis Tools](../timing/) — the *measurement* side: extracting cycle counts and bus spans
  from a VCD / cosim run (where the datapoints come from).
- [Fitting a timing model](../timing_model/fit.md) — the conceptual fit (recovering `latency` / `ii`
  from a line) that `LinCalibModel` performs.
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector that names the
  platform a build synthesizes and calibrates for.
