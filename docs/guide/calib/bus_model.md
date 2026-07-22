---
title: The bus-transfer model
parent: Timing model fitting
nav_order: 7
audience: python
api: [BusCalib, BusTiming, CalibBusStep]
summary: "BusCalib fits the platform's m_axi transfer law — span(num_trans, nwords) per direction — and stores it in mm_bus.json beside the platform manifest. The datapoints are measured component-independently off the memory port (measure_bus_span groups beats into transfers by idle gaps); a sweep accumulates a per-run corpus (add_run -> points/) then fits. bus_timing() hands back a configured BusTiming the memory slave charges during pysim, or an unconfigured one (word_bw fallback) on an uncalibrated platform. CalibBusStep automates it as a DAG step."
---
# The bus-transfer model

The first level of the [two-level split](./index.md#two-levels-what-is-a-platform-property-what-is-a-component-property):
how long the `m_axi` interconnect takes to move `n` words in `k` bursts. This is a property of the
**platform** — the memory system and AXI adapter — so it is fit **once** and every accelerator on that
platform reuses it. `BusCalib` is that fit; `mm_bus.json` is where it lands.

## The law

A contiguous transfer of `n` words is issued as `k = ceil(n / MEM_AXI_MAX_BURST)` bursts (HLS's
`max_burst_length` is 16). `BusCalib` fits a per-direction span from those two features:

```
span(num_trans, nwords)  =  a·num_trans  +  b·nwords  +  c        # cycles
```

On the reference BFM platform the measured laws are `nwords + (num_trans − 1)` (read) and
`nwords + 2·(num_trans − 1)` (write) — one cycle per word, plus a per-burst-boundary gap read straight
off the trace. The intercept and coefficients capture whatever the *real* interconnect does; an
idealized BFM gives these clean laws, a real DDR controller would widen them — which is the point of
*measuring* rather than assuming.

## Measuring, component-independently

The bus law is read off the **memory port**, not any kernel's firing — so it is genuinely a platform
property, independent of which accelerator generated the traffic. `measure_bus_span` does this:

```python
from waveflow.calib.bus_model import measure_bus_span

point = measure_bus_span(bound_trace, "gmem1", "write")
# -> {"num_trans": 32, "nwords": 512, "span": 574}
```

It samples the bundle's AR/AW/R/W handshakes from the trace, groups the beats into per-transfer runs by
idle gaps (a gap wider than `idle_gap` ends a transfer), and returns the **median** transfer's
`{num_trans, nwords, span}`. Measuring one transfer — not the whole run — keeps inter-firing idle out of
the bus span.

## Fitting and storing

`BusCalib` accumulates a per-run corpus across a sweep, then fits:

```python
from waveflow.calib.bus_model import BusCalib

bus = BusCalib(platform_dir="calib/work/zynq7020_bfm_100mhz", clk_freq=100e6)
bus.add_run("n128", write={"num_trans": 8,  "nwords": 128, "span": 142})   # -> points/n128.json
bus.add_run("n512", write={"num_trans": 32, "nwords": 512, "span": 574})   # -> points/n512.json
bus.fit()                                                                  # no args -> read the corpus
```

- `add_run(run_id, read=…, write=…)` writes one distilled `points/<run_id>.json` — a re-run overwrites
  its own point, so the corpus is concurrency-safe and a sweep is `add_run`-per-size then one `fit()`.
- `fit()` with no arguments reads the accumulated corpus; `fit(read_points=…, write_points=…)` takes
  points directly (the one-shot case). A direction with no points is simply absent — a write-only
  accelerator calibrates only the write channel.
- The result is written to `mm_bus.json`:

```json
{ "clk_freq": 100000000.0, "basis": ["num_trans", "nwords"],
  "models": { "read":  { "num_trans": 0.066, "nwords": 1.058, "intercept": -1.0 },
              "write": { "num_trans": 0.070, "nwords": 1.121, "intercept": -2.0 } } }
```

A bus law needs **≥2 distinct sizes** to fit the slope, so a sweep is the natural unit. (With only two
*proportional* sizes `num_trans` and `nwords` are collinear — see the
[workflow&#39;s note](./workflow.md#a-known-limitation-collinear-sizes).)

## Deploying: `bus_timing()`

The calibration side fits and persists; the *runtime* side is a `BusTiming` the memory slave consults
during pysim. `bus_timing()` bridges them — `load_or_default`:

```python
bt = BusCalib(platform_dir="calib/platforms/zynq7020_bfm_100mhz").bus_timing()
```

- On a **calibrated** platform it returns a `BusTiming` configured from `mm_bus.json`, so pysim charges
  the real burst cost.
- On an **uncalibrated** platform (no `mm_bus.json`) it returns an *unconfigured* `BusTiming` — both
  directions `None` — so a slice degrades to the plain per-word span rather than crashing.

Charging the bus term in pysim is what lets the [component residual](./component_residual.md) shrink to
the component's own control cost: on the reference platform the writer's residual drops from ~36
(bus + control lumped) to ~22 (control only), and the 14-cycle burst term moves onto this shared model.

## Automating it: `CalibBusStep`

In a build DAG, `CalibBusStep` does the measure → `add_run` → refit loop from a traced run — no design
factory, because the bus law is in the trace, not a pysim run:

```
… -> RtlSimStep -> trace (manifest + vcd) -> CalibBusStep   (per m_axi bundle: measure_bus_span -> add_run -> fit)
```

It consumes the [trace manifest + VCD](../timing/trace_steps.md), walks each boundary `m_axi` bundle,
measures the direction(s) it carries, adds the run to the platform corpus, and refits `mm_bus.json`.
Across a sweep (one step per size) the corpus grows until the model fits.

## See also

- [Platforms](./platform.md) — where `mm_bus.json` lives and how the platform is keyed.
- [Component residuals](./component_residual.md) — the second level, which assumes the bus term is
  already charged.
- [`BusTiming` / AXI-MM timing](../timing/aximm.md) — the runtime model the slave applies.
- [Tracing a kernel run](../timing/trace_steps.md) — where `measure_bus_span`'s input comes from.
