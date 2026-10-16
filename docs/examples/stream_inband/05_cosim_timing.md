---
title: RTL Cosim Timing Verification
parent: Streaming polynomial
nav_order: 9
summary: "What co-simulation tells us that C simulation cannot: how many clock cycles the RTL takes. The cosim report's cycle count for one kernel call is compared, at 32 and 64 bits, with the count pysim predicts for the same span, and the build fails if they differ by more than 20 cycles. The pysim timing is poly's declared timed phases, fitted from a traced cosim: every stretch of a command matches the RTL except a one-cycle restart per command, and the start/done handshake is outside the module's timing; both are counted here. Includes the history: a hand-set proc_latency, and why the measured span had to match before any number meant anything."
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
| 32 bits | 145 | 152 | 7 | 20 |
| 64 bits | 88 | 94 | 6 | 20 |

Each verdict file (`results/timing_verdict_w*.json`) is written whether the check passes or fails,
so downstream tools can read the numbers without re-running the build.

The 64-bit kernel is about 58 cycles faster: at 64 bits the 100 samples take 50 words instead of 100,
and each 6-word command header takes 3.  The kernel is bandwidth-bound -- one word a cycle, whatever
the width -- and the model counts words the same way.

## Where the pysim cycles come from

Poly declares its timing as two [timed phases](../../guide/timing_model/timed_phases.md) -- the
response after a command header, and the samples-to-results pipeline (a latency and a fill) -- and
they are **fitted from a traced cosim**, not set by hand
(`examples/stream_inband/poly_calibrate.py`; [Calibrating a timed
phase](../../guide/timing_model/fit.md)):

| span | 32 bits | 64 bits |
|---|---|---|
| `resp_time` -- header's last beat to response's first | 4 | 4 |
| `latency` -- first sample word to first result word | 34 | 33 |
| `fill_time` -- first to last result word | `nwords - 1` | `nwords - 1` |

Compared beat by beat with the trace, every stretch inside a command matches the RTL: the response
four cycles after the header, the samples starting **one** cycle after it -- the RTL sends the
response while it reads the samples, and so does poly's body -- and the results 34 cycles after the
first sample, at one word a cycle.  The 7 cycles pysim is short are counted, and deliberately left:

- **one cycle per command**, the kernel's loop restarting between commands (the next header ends six
  cycles after the last result at RTL, five in pysim) -- no declared span holds it, and stating one
  would cost more than the cycle is worth;
- **about six cycles at the call's start and end** -- `ap_start` to the first header beat, and the END
  header to `ap_done`.  That is the AXI-Lite handshake of a host-activated kernel; a module's timed
  phases cover its own work on its streams, not its control handshake.

So the gap is a known, counted quantity, not a parameter absorbing an unknown one -- which is the
point of the history below.

## How the model used to be calibrated

pysim's timing model used to be two hand-set numbers: `proc_ii`, the cycles per sample word, and
`proc_latency`, the fixed overhead of a call -- the pipeline fill of the sample loop plus the stream
handshakes.  They now only seed the phases before a fit.

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
header word.  With the same `proc_latency = 40` it predicted 147 against 152 at 32 bits, and exactly
94 at 64.

That 147 looked close for the wrong reason.  Once the phases were fitted span by span, the hand-set
numbers turned out wrong on every span -- latency 40 against 34, response 0 against 4, a fill one cycle
long -- with errors that happened to cancel.  The lesson for your own design: before you fit a timing
parameter, make sure the model and the RTL measure the same interval; and fit the intervals one by one,
so an end-to-end number cannot be right by accident.

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
   fill?  Why is that fragile -- and why did fitting the spans one by one move the total *away* from
   cosim?
3. The 64-bit kernel is about 55 cycles faster on the timing scenario.  Where do most of them come
   from?

---

Next: [Reading the protocol off a waveform →](./poly_axi_stream.md)
