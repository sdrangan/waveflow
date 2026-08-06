---
title: RTL Cosim Timing Verification
parent: Streaming polynomial
nav_order: 5
---

# RTL cosim timing verification

The fifth and final group closes the cycle-approximate-Python loop:
it reads the cycle count the RTL co-simulator measured for one
transaction and compares it against the cycle count the Python
golden predicted in Group 1.  When the two agree within tolerance,
the SimPy timing model has been *experimentally validated* against
the RTL — the strongest claim Waveflow can make about its sim.

| Step | Produces | What it does |
|------|----------|--------------|
| `extract_cosim_timing` | `cosim_timing` | Runs `waveflow.utils.cosimparse.CosimReportParser` on the solution dir; serializes the kernel's measured transaction-cycle count to `results/cosim_timing.json` |
| `validate_timing` | `timing_verdict` | Compares `py_timing.transaction_cycles` (Group 1) against `cosim_timing.transaction_cycles`; raises if the absolute delta exceeds `tolerance_cycles` (default 20) |

## The structured timing pair

Both timing-side steps deliberately emit *structured* JSON with
named cycle counts — not pass/fail bits.  This shape is what the
future model-training workflow (per
[`project-cycle-model-training`](#whats-next)) will consume to fit
`HwModule` timing parameters from a corpus of cosim runs.

`results/py_timing.json` (from Group 1):

```json
{
    "transaction_cycles": 140,
    "transaction_seconds": 1.4e-06,
    "clk_freq": 100000000.0,
    "source": "py_sim",
    "events": { "samp_read_begin": ..., "samp_out_write_end": ... }
}
```

`results/cosim_timing.json` (this group):

```json
{
    "transaction_cycles": 144,
    "report_path": ".../sim/report/poly_cosim.rpt",
    "vitis_version": "2025.1+",
    "source": "cosim",
    "top": "poly"
}
```

The parser handles both Vitis 2025.1+ (`<top>_cosim.rpt` table) and
the legacy `cosim.log` shape — picked transparently per file presence.

## The verdict

`ValidateTimingStep` produces `results/timing_verdict.json`
regardless of pass/fail:

```json
{
    "pass": true,
    "py_cycles": 140,
    "cosim_cycles": 144,
    "delta": 4,
    "tolerance": 20,
    "py_timing_path": "...",
    "cosim_timing_path": "..."
}
```

If `delta > tolerance` the build fails with `RuntimeError`.  Either
way the verdict file is written first — downstream tools can read
the actual numbers without re-running the build.

The tolerance is a constructor parameter on the step (defaulted to
20).  Tightening it is a one-line change once the model is refined.

## How the model was calibrated

The first cosim run reported 144 cycles for `nsamp=100`,
`unroll_factor=1`, `in_bw=32`.  The Python sim with the default
`proc_latency=10` reported 110 — delta=34, over tolerance.

The fix was to bump `PolyAccel.proc_latency` from 10 to 40
to absorb the RTL pipeline fill/drain and stream handshake overhead
that the simpler "compute-latency" model didn't capture.  With that
calibration:

- `py_cycles = 140`
- `cosim_cycles = 144`
- `delta = 4`

This is the manual v1 of the model-training workflow described next.

## What's next

The structured timing artifacts emitted here are the input format
for a future parameter-fitting step that will fit
`HwModule.proc_latency` / `proc_ii` / similar from a corpus of
cosim runs across `param_supports` variants.  Once that lands,
calibration stops being a manual one-line edit and starts being a
build-DAG step in its own right.

## Run the whole pipeline

```bash
python -m examples.stream_inband.poly_build --through validate_timing --force --live-output
```

Requires Vitis HLS on `PATH`.  Produces `results/timing_verdict.json`
with a green `pass=true` when the model and the RTL agree.

---

Back to: [Polynomial Accelerator overview](./index.md)
