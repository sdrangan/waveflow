---
title: RTL Cosim Timing Verification
parent: Streaming polynomial
nav_order: 9
summary: "What co-simulation tells us that C simulation cannot: how many clock cycles the RTL takes. The cosim report's cycle count for one kernel call is compared, at 32 and 64 bits, with the count the pysim timing model predicts for the same span, and the build fails if they differ by more than 20 cycles. Includes how the model was calibrated: proc_latency from 10 to 40, and why the measured span had to match before the numbers meant anything."
---

# RTL co-simulation timing

What does co-simulation tell us that C simulation cannot?  **Time.**  C simulation runs the
kernel as a C++ function: it checks *what* comes out, never *when*.  Co-simulation runs the
synthesized RTL, clock cycle by clock cycle, on the same stimulus, so it measures how long the
kernel takes.  This group compares that measurement with what the Python timing model predicted, at
both widths.

| Step | Produces | What it does |
|------|----------|--------------|
| `check_cosim` | `check_cosim` | The co-simulated response of the timing scenario, at each width, checked bit-exactly against its expected response, like every other stage |
| `extract_cosim_timing_w32`, `_w64` | `cosim_timing_w*` | Runs `waveflow.utils.cosimparse.CosimReportParser` on the solution and writes the measured cycle count to `results/cosim_timing_w*.json` |
| `validate_timing_w32`, `_w64` | `timing_verdict_w*` | Compares pysim's count (`results/py_timing_w*.json`) with the RTL's, and fails if they differ by more than `tolerance_cycles` (20) |
| `error_vcd` | `vcd/error_path.vcd` | Co-simulates `early_tlast_vcd` with port tracing, for the [error-path](./error_path.md) waveform |

## Measure the same thing on both sides

The timing scenario is one `DATA` command of 100 samples, then `END`: one kernel call.  The cosim
report gives the latency of that call, from `ap_start` to `ap_done`.  pysim must measure **the same
span**, from the start of its `body()` to its return, or the comparison means nothing.

## The numbers

| Width | pysim | cosim | delta | tolerance |
|---|---|---|---|---|
| 32 bits | 147 | 152 | 5 | 20 |
| 64 bits | 94 | 94 | 0 | 20 |

`results/timing_verdict_w32.json`:

```json
{
    "pass": true,
    "py_cycles": 147,
    "cosim_cycles": 152,
    "delta": 5,
    "tolerance": 20
}
```

Each verdict file is written whether the check passes or fails, so downstream tools can read the
numbers without re-running the build.

The 64-bit kernel is 58 cycles faster in cosim (53 in pysim).  At 64 bits the 100 samples take 50
words instead of 100, and each 6-word command header takes 3.  The kernel is bandwidth-bound: one
word per cycle, whatever the width.  The model counts words the same way, which is why one
`proc_latency` fits both widths.

## How the model was calibrated

pysim's timing model has two numbers: `proc_ii`, the cycles per sample word (1, matching the
pipelined loop's II), and `proc_latency`, the fixed overhead of a call -- the pipeline fill of the
29-stage sample loop plus the stream handshakes.

**First calibration: 10 → 40.**  The first co-simulation reported 144 cycles for 100 samples at
32 bits.  pysim, with a guessed `proc_latency = 10`, predicted 110: a delta of 34, outside the
tolerance.  The guess counted only the arithmetic latency.  It missed the pipeline fill and drain
and the stream handshakes.  Setting `proc_latency = 40` brought pysim to 140 against cosim's 144, a
delta of 4.  That is the method: run the RTL, read the measured number, set the model's parameter
from it, never from a guess.

**Second: make the spans match.**  At that point pysim measured only from the first sample read to
the last result written, while cosim measured the whole call, and `proc_latency` silently absorbed
the difference.  A parameter that absorbs a mismatch only works until something changes it.  When
the command header grew to carry the coefficients (6 words instead of 2 at 32 bits), cosim's span
grew and pysim's did not.  So pysim now measures the whole call, as cosim does, and models every
header word.  With the same `proc_latency = 40` it predicts 147 against 152 at 32 bits, and exactly
94 at 64.  The model did not need recalibrating, because it now counts the same cycles as the RTL.

The lesson for your own design: before you fit a timing parameter, make sure the model and the RTL
measure the same interval.  Otherwise the parameter fits the mismatch, not the hardware.

## What's next

The structured timing files are the input for a future parameter-fitting step: fitting
`proc_latency`, `proc_ii` and similar from a corpus of cosim runs across `param_supports` variants.
Calibration then stops being a manual edit and becomes a build step.

## Run the whole pipeline

```bash
python examples/stream_inband/poly_build.py --through summary
```

Requires Vitis HLS.  `results/summary.json` collects every check, both synthesis reports and both
timing verdicts.  (`--through validate_timing_w32` alone would skip the cosim response check and the
other width: a target runs only its ancestors.)

## Check your understanding

1. Why can C simulation not catch a kernel whose sample loop runs at II = 2?
2. Before the spans were matched, which quantity was `proc_latency` absorbing besides the pipeline
   fill?  Why is that fragile?
3. The 64-bit kernel is about 55 cycles faster on the timing scenario.  Where do most of them come
   from?

---

Next: [Reading the protocol off a waveform →](./poly_axi_stream.md)
