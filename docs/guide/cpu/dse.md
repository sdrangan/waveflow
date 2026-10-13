---
title: Using it in a DSE
parent: Processor Model
nav_order: 3
audience: python
snippets: run
api: [CpuPlatform, CpuPlatform.cpu_config, CpuPlatform.sw_function, CpuPlatform.area_model, CpuAreaModel.estimate]
summary: "Using the calibrated processor in a design-space exploration: a CpuPlatform turns the shipped A53 calibration into configurations (core count, caches, static power, switch cost), calibrated software functions and an area model, each with its confidence. The cache-regime features carry the cycle models to other cache sizes without refitting; the known limits are small operations, back-to-back sequences and the gem5/McPAT approximations."
---

# Using it in a DSE

A design-space exploration asks the same questions of many configurations. `CpuPlatform` answers the
processor's share of them from the shipped calibration:

```python
from waveflow.cpu.platform import CpuPlatform

platform = CpuPlatform.load()   # a53_hpi_1200mhz_gem5v25_1
for cores in (1, 2, 4):
    cfg = platform.cpu_config(n_cores=cores)
    est = platform.area_model().estimate(cfg)
    print(f"{cores} core(s): {est['area_mm2'].value:.2f} mm2, "
          f"{cores * cfg.static_power_mw:.0f} mW static, "
          f"switch {cfg.switch_cycles:.1f} cycles, {est['area_mm2'].level.value}")
```

```text
1 core(s): 3.07 mm2, 97 mW static, switch 81.8 cycles, INTERPOLATED
2 core(s): 4.12 mm2, 144 mW static, switch 81.8 cycles, INTERPOLATED
4 core(s): 6.20 mm2, 239 mW static, switch 81.8 cycles, INTERPOLATED
```

- `cpu_config(n_cores, ...)` — a `CpuConfig` at the platform's clock, with the context-switch cost
  from the `ctx_switch` calibration and the static power from the leakage model. Cache sizes are
  fields like any other.
- `sw_function(family)` — a calibrated function per family (`sched_ops.add`, `cdot_q15`, ...): the
  Python twin, priced by the fitted cycle and energy models, with its code size. Your own software
  uses the same models when it does the same work and reports the same counters — the
  [example](../../examples/cpu_sched/index.md)'s scheduler keeps a real ready list with the
  calibrated algorithm and is priced by the `sched_ops` models.
- `area_model().estimate(cfg)` — area (mm²) and leakage (mW, the whole configuration), from McPAT
  over 1–4 cores, 16–64 KB L1 and 256 KB–2 MB L2.

## Other cache sizes

The cycle models were fitted at the A53's 32 KB L1D and 1 MB L2. Their cache-sensitive features are
expressed against the configuration — the working set beyond L1, beyond L2 — so a configuration with
other caches is priced without refitting. At 16 KB L1D and 512 KB L2, gem5 measured `gather_hist`
1.61x slower than at the reference, and the unchanged model predicted it within 6.9 % (median 2.7 %).
The cache-insensitive families measured the same at both.

## Read the confidence

A configuration or a workload outside the measured region is reported `EXTRAPOLATED`, with the
feature that left the range and by how much; `FunctionStats.levels` says for how many calls. The
shipped platform's limits, measured:

- **small operations** — single calls of a few tens of cycles can be off by up to about 40 %;
- **back-to-back sequences** — the model over-prices a scheduler running operations back to back by
  about 20 % in total, a near-constant 20–40 cycles per call ([Calibration](calibration.md));
- **the ground truth** is gem5's HPI and McPAT at 22 nm, without an operating system or DRAM energy.

A DSE that ranks configurations by processor load is less exposed than one that needs absolute
latencies of single small calls.

## Alongside software threads

`plans/host_runtime.md` makes host software `SwThread`s with an explicit `compute(cycles)`. The
processor here is the CPU resource such a `compute` can draw on: `Processor.compute(cycles)` already
takes a fixed cycle count, and an adapter for `SwThread` is planned once that work lands.
