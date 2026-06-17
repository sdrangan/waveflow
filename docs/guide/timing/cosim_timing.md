---
title: Cosim timing
parent: Timing Analysis Tools
nav_order: 6
---

# Cosim timing

## Concept

Cycle-timing validation compares Python timing measurements against Vitis cosim reports and records a structured verdict. The flow combines Python-side extraction, cosim report parsing, and tolerance-based comparison.

This keeps timing checks reproducible and machine-readable: build runs always produce JSON artifacts, and failures halt with explicit delta/tolerance diagnostics.

## API

- [`ExtractPyTimingStep`](../../../waveflow/build/verify_steps.py) produces Python timing JSON.
- [`ExtractCosimTimingStep`](../../../waveflow/build/cosim_steps.py) parses cosim report data.
- [`ValidateTimingStep`](../../../waveflow/build/cosim_steps.py) emits `timing_verdict` and enforces tolerance.
- [`CosimReportParser`](../../../waveflow/utils/cosimparse.py) handles 2025.1+ `*_cosim.rpt` and legacy `cosim.log`.

## Example

From [`examples/stream_inband/poly_build.py`](../../../examples/stream_inband/poly_build.py), the timing-check segment wires the three-step chain:

```python
outer_dag.add(ExtractPyTimingStep(...))
outer_dag.add(ExtractCosimTimingStep(top=top_name, report_dir_artifact="report_dir"))
outer_dag.add(ValidateTimingStep(tolerance_cycles=timing_tol_cycles))
```

On the poly reference run from PR #31, the recorded result reports a `delta=4` cycle difference between Python and RTL cosim.

## Quick reference

- Cosim parser prefers `<top>_cosim.rpt`, then falls back to `cosim.log`.
- `ValidateTimingStep` always writes `timing_verdict.json`.
- Pass rule: `abs(py_cycles - cosim_cycles) <= tolerance_cycles`.
- Failure raises `RuntimeError` after writing verdict output.
- Use structured artifacts for regression tracking and future model fitting.
