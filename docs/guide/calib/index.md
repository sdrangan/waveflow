---
title: Timing model fitting
parent: Guide
nav_order: 13
has_children: true
audience: python
api: [CalibDataFrame, CalibModel, LinCalibModel, InterpCalibModel, StreamTimingModel, BusCalib, Platform]
summary: "Fit a timing model's parameters from measurement so the fast loosely-timed sim tracks the slow RTL. Two methods: DIRECT — fit the whole cycle count from a (size, cycles) sweep (a LinCalibModel recovers a loop's latency/ii); and RESIDUAL — fit only the gap between RTL and a pysim that already times its transfers (a component's control cost). Same CalibModel / CalibDataFrame machinery underneath. Fits live either in a project-local dir (custom components) or a git-tracked platform library keyed by FPGA part + clock (shared infra), where the m_axi bus law is also fit once and reused."
---

# Timing model fitting

A [timing model](../timing_model/) predicts a component's timeline from a few numbers — a `latency`, an
initiation interval, a per-transfer cost. Those numbers are properties of the *synthesized* hardware.
**Fitting** recovers them from measurement: run the kernel through synthesis / RTL cosim, record the
cycles, and fit a model, so the fast LT simulation reproduces the slow RTL without you transcribing
report numbers by hand.

## Two methods: direct and residual

There are two ways to fit, and this section is organized around them:

- **Direct** — fit the model's parameters straight from a sweep of `(size, cycles)` measurements. A
  [loop model](../timing_model/loops.md)'s `latency` / `ii` are the two coefficients of a line, which a
  `LinCalibModel` recovers. This is the simple case: [Fitting a timing model](./fit.md).
- **Residual** — when a component's loosely-timed sim *already* charges most of its time (its transfers
  are timed by the interfaces), fit only the **gap** between the RTL and the pysim — the small control
  cost pysim misses. This is [Component residuals](./component_residual.md).

Both are the same machinery underneath — a [`CalibModel`](./models.md) over a
[`CalibDataFrame`](./dataframe.md) corpus — differing only in *what* they fit: the whole cycle count, or
the residual.

## The two-level split: bus vs component

For a component that moves data over `m_axi`, the residual method leans on a split. The run's cost is:

```
    RTL cycles  =  bus transfer  +  component control
                    └─ PLATFORM ─┘   └── COMPONENT ──┘
```

The **bus transfer** — how long the interconnect takes to move `n` words in `k` bursts — is a property
of the **platform** (memory system + AXI adapter), so it is fit **once per platform** and reused by
every accelerator ([`BusCalib`](./bus_model.md)). With that charged in pysim, the **component control**
residual shrinks to the kernel's own overhead, fit per `(component, platform)`
([`StreamTimingModel`](./component_residual.md)).

## Where the fit lives: custom vs shared infra

A fit is stored in one of two places, chosen by one knob:

- **Custom components** — your accelerator's own kernels. The fit is specific to your design, so it goes
  in a **project-local directory you pick** (`calib_dir`).
- **Shared-infra components** — reusable framework kernels (`MemRStream` / `MemWStream`, …). Their fit
  is a `(component, platform)` property, so it goes in a **git-tracked platform library** (`platform_dir`)
  and ships with the repo — reuse the component on a calibrated platform and inherit its timing with
  **no re-calibration**. The library is keyed by an FPGA-part identity (see [Platforms](./platform.md))
  and populated through a [two-tier work → publish flow](./workflow.md).

## Everything is in cycles

Fitted numbers are stored in **cycles**, not seconds, so the artifact is clock-independent — a re-deploy
at a different *simulation* frequency needs no refit. The one clock that *does* change the numbers is the
**synthesis** clock (`create_clock -period`): HLS schedules to meet it, so a different target period can
change the cycle counts. That is why a platform is keyed by part **and** clock — see
[Platforms](./platform.md).

## In this section

The direct method and its primitives first, then the residual method and the platform infrastructure:

- [Fitting a timing model](./fit.md) — the direct method: recover a loop model's `latency` / `ii` from a
  `(size, cycles)` sweep, and validate on a held-out point.
- [Models](./models.md) — the per-target fit / predict / score interface (`CalibModel`), the linear
  model (`LinCalibModel`), and the calibrated lookup (`InterpCalibModel`).
- [The corpus — `CalibDataFrame`](./dataframe.md) — one timestamped row per measurement, a
  `pandas.DataFrame` under `.df`, with `save` / `load`.
- [A worked example](./example.md) — the primitive fit mechanics: fit a `LinCalibModel`, score it, hold
  a point out; then a saturating curve with `InterpCalibModel`.
- [Component residuals](./component_residual.md) — the residual method: `StreamTimingModel`, fit per
  `(component, platform)` with the bus term already charged.
- [Platforms](./platform.md) — the platform identity (`platform.json`), why it is keyed by FPGA part
  **and** synthesis clock, and how `BuildConfig` selects and confirms one.
- [The bus-transfer model](./bus_model.md) — `BusCalib`: the `m_axi` span law fit once per platform,
  measured component-independently off the ports.
- [The mem-stream residual](./memstream.md) — the reusable `MemRStream` / `MemWStream` control residual,
  and the per-component **fixture** (`waveflow/calib/fixtures/`) that fits it.
- [The calibration workflow](./workflow.md) — the two-tier `work` → `publish_calib` → `platforms` flow,
  the DAG steps, and the reference `zynq7020_bfm_100mhz` platform end to end.

## See also

- [Timing Models](../timing_model/) — the forward models whose parameters this section fits, and where a
  model is [attached to a component](../timing_model/insertion.md).
- [Timing Analysis Tools](../timing/) — the *measurement* side: extracting cycle counts and bus spans
  from a VCD / cosim run (where the datapoints come from).
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector that names the
  platform a build synthesizes and calibrates for.
