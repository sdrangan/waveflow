---
title: The mem-stream residual
parent: Timing model fitting
nav_order: 9
audience: python
api: [MemRStream, MemWStream, StreamTimingModel, ComponentFixture, MemRStreamFixture, MemWStreamFixture]
summary: "The reusable m_axi mem-streams (MemRStream / MemWStream) are timed by the bus law plus a per-firing CONTROL residual — a (component, platform) property fit once and reused by every accelerator. The residual is fit by a per-component FIXTURE under waveflow/calib/fixtures/ (mem_w_stream.py, mem_r_stream.py): a ComponentFixture declares the component id, the regression basis, a size sweep, a run_pysim that drives the component STANDALONE (StreamDriver -> component -> sink, bus law on the memory so the residual is control-only), and rtl_firings (measured spans or None). fixture.calibrate(platform) sweeps, collects RTL + pysim, and fits into components/<id>/params.json. An accelerator loads it by pointing its mem stages at the platform. memcpy is writer-bound so only the writer fixture existed; the reader-bound interleaver is what forced mem_r_stream.py."
---
# The mem-stream residual

The framework `MemRStream` / `MemWStream` are the reusable `m_axi` read/write owners an accelerator
composes ([the mem-stream pattern](../../examples/memcpy/memcpy.md)). Their timing is the
[two-level split](./index.md#two-levels-what-is-a-platform-property-what-is-a-component-property) in
miniature: the [bus law](./bus_model.md) charges the *transfer*, and a per-firing **control residual**
charges everything else the firing does around it. This page is that residual for the mem-streams — and,
the part documented nowhere else, **how it is calibrated: a per-component fixture.**

## What the residual is

A mem-stream firing moves `nwords` words, and the bus law already accounts for the burst transfer. But the
firing occupies more cycles than the transfer alone — issuing the command, relaying the in-band descriptor,
the `ap_done` handshake, and (for the writer) the posted-write drain. That gap is the residual:

```
residual  =  rtl_firing_span  −  (bus-charged pysim span)      # the component's own control cost
```

It is small and roughly constant — on the reference BFM platform ~22 cycles for the writer (it models the
posted-write drain) and ~15 for the reader (its own control cost). A
[`StreamTimingModel`](./component_residual.md) captures it and charges it per firing via `timed_delay`, on
top of the bus term. This is exactly the [component residual](./component_residual.md) method; what follows
is how you *produce* one for a mem-stream.

## The fixture: how it is calibrated

The residual is a `(component, platform)` property — fit **once** per platform, reused by every accelerator
on it. The fit vehicle is a **per-component fixture** under `waveflow/calib/fixtures/`:

- [`mem_w_stream.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/calib/fixtures/mem_w_stream.py) — the writer (`MemWStreamFixture`).
- [`mem_r_stream.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/calib/fixtures/mem_r_stream.py) — the reader (`MemRStreamFixture`).

Each is a `ComponentFixture` (`waveflow/calib/fixture.py`) that declares four things:

- **`component`** — the task-body id its firings carry, which keys the platform-library subdir
  (`mem_r_stream_framed_task`, `mem_w_stream_framed_done_task`).
- **`basis`** — the regression features. The writer's is `[nwords, num_trans, n_fwd]` (it models a
  per-forwarded-message echo cost); the reader's is just `[nwords, num_trans]` (it relays the descriptor
  but does not model a per-burst relay cost — the 1-word relay folds into the intercept).
- **`sweep()`** — the grid of sizes (vary each feature independently, so their coefficients stay
  separable).
- **`run_pysim()`** — runs the component **standalone** for one point and returns its `firing_records`.
- **`rtl_firings()`** — the measured RTL spans for a point, or `None` where a cosim is still needed
  (reported, never invented).

`calibrate(platform)` composes those into the fit — sweep, collect RTL + pysim into the platform's
component dir, and fit:

```python
from waveflow.calib.fixtures.mem_r_stream import MemRStreamFixture
from waveflow.calib.platform import Platform
from waveflow.hw.clock import Clock

plat = Platform.resolve("calib/work", "zynq7020_bfm_100mhz")   # bus law must already be fit (below)
report = MemRStreamFixture().calibrate(plat, clk=Clock(freq=100e6))
# -> components/mem_r_stream_framed_task/params.json     the fitted residual
# report: {"component": …, "fitted": True, "params": {…}, "missing_rtl": [], "coverage": {…}}
```

Fit the [bus law](./bus_model.md) **first**: `run_pysim` loads it onto the fixture's memory, so the pysim
already charges the transfer and the residual comes out as the component's own control cost — not the bus
term lumped in.

## Standalone, so it is reusable

The fixture drives the component **alone** — never through an accelerator — because the residual is the
component's own cost, independent of what composes it. `run_pysim` wires a
[`StreamDriver`](../timing/trace_steps.md) that feeds the command frame, a memory behind `m_mem`, and a
sink that drains the output:

```
StreamDriver → s_cmd → [ MemRStream ] → m_out → StreamSink        # the reader vehicle
                          │  m_mem
                          ▼
                    MemoryMod  (bus law loaded → residual is control-only)
```

Only the package is needed — no example — which is what lets the same fit serve every accelerator on the
platform.

## Loading it

An accelerator loads the residual by pointing its mem stages at the platform:

```python
MemRStream(mem_dwidth=64, inband=True, platform_dir="…/zynq7020_bfm_100mhz")
```

`_resolve_calib_dir` maps `(platform_dir, component_id)` to the library slot; if a `params.json` is there,
a `StreamTimingModel` loads it and charges the delay. **No residual shipped → residual-free**: the stage
runs on the bus law alone (a graceful, if slightly optimistic, degrade).

## Why the reader fixture exists

The two fixtures were not written at once, and the reason is a good lesson. `mem_copy` is **writer-bound**
— its writer is the bottleneck — so calibrating the *writer* made its pysim match the RTL exactly, and the
reader's residual was never needed. Nothing was reader-bound until the **interleaver**: a gather that
issues *two* reads per job, so the **reader** sets the period. It ran ~10% under the RTL until
`mem_r_stream.py` was added — bringing it to ~1%. The rule the pair illustrates: **calibrate the stage a
design actually bottlenecks on**; an un-fit residual only hurts where it is on the critical path.

## See also

- [The bus-transfer model](./bus_model.md) — level 1, the transfer law this residual sits on top of.
- [Component residuals](./component_residual.md) — the general residual method (`StreamTimingModel`,
  `collect_rtl` / `collect_pysim` / `fit`) the fixture drives.
- [Platforms](./platform.md) — where `components/<id>/params.json` lives and how the slot is keyed.
- [Fitting the timing model](../../examples/interleaver/timing_fit.md) — a design fitting its *own* custom
  stage on top of these shipped infra residuals.
