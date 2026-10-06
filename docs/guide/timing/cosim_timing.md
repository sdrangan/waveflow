---
title: Cosim timing
parent: Timing Analysis Tools
nav_order: 8
summary: "Comparing the Python timing estimate against the cycle count Vitis cosim reports, as a build step with a recorded verdict. Three steps chain: extract the Python timing, parse the cosim report, and compare within a tolerance — always writing timing_verdict.json, and raising after writing it when the difference exceeds the tolerance."
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

From [`examples/stream_inband/poly_build.py`](../../../examples/stream_inband/poly_build.py), the timing-check segment wires the three-step chain, once per stream width:

```python
for w in WIDTHS:
    dag.add(ExtractPyTimingStep(name=f"extract_py_timing_w{w}", word_bw=w))
    dag.add(ExtractCosimTimingStep(
        name=f"extract_cosim_timing_w{w}", top=TOPS[w],
        report_dir_artifact=f"report_dir_w{w}", cosim_timing_artifact=f"cosim_timing_w{w}",
        output_path=f"results/cosim_timing_w{w}.json"))
    dag.add(ValidateTimingStep(
        name=f"validate_timing_w{w}", py_timing_artifact=f"py_timing_w{w}",
        cosim_timing_artifact=f"cosim_timing_w{w}", tolerance_cycles=20,
        output_path=f"results/timing_verdict_w{w}.json",
        verdict_artifact=f"timing_verdict_w{w}"))
```

On that example pysim predicts 147 cycles against cosim's 152 at 32 bits (delta 5), and 94 against
94 at 64 bits -- both measured over one whole kernel call.  The example's
[calibration story](../../examples/stream_inband/05_cosim_timing.md#how-the-model-was-calibrated)
shows why the two sides must measure the same span.

## Quick reference

- Cosim parser prefers `<top>_cosim.rpt`, then falls back to `cosim.log`.
- `ValidateTimingStep` always writes `timing_verdict.json`.
- Pass rule: `abs(py_cycles - cosim_cycles) <= tolerance_cycles`.
- Failure raises `RuntimeError` after writing verdict output.
- Use structured artifacts for regression tracking and future model fitting.
