---
title: The matched filter on the vector unit, timing at RTL
parent: A matched filter as a vector program
nav_order: 1
summary: "What the RTL measured (Vitis HLS / Vivado xsim 2025.1, xczu48dr, 4 ns) for the matched filter on the vector unit, across antenna counts, chunk lengths (VLMAX 16, 64, 256), word widths and lanes: the execute task binds at short chunks (3.1 times the input floor at VLMAX 16, 1.27 at 64), the loader at long ones (1.12 at VLMAX 256) and at L 8 (1.15); 128-bit words change nothing while the execute task binds. What a trace shows, and pysim's known miss: 3 to 6% over where the execute task binds, 11% under where the loader does."
---

# Timing at RTL

Every number on this page is an RTL run of the unit in its harness, bit-exact against pysim, from
`examples/vecunit_mf/mf_measure.py` (Vitis HLS / Vivado xsim 2025.1, `xczu48dr` at 4 ns, csynth's
estimated clock 3.12 to 3.14 ns on every build):

```bash
python -m examples.vecunit_mf.mf_measure --root /tmp/vu_mf_m     # -> measured.json, 12 runs, 5 builds
```

## The frames against their bounds

A frame's time is set by the slowest of the unit's three tasks: the loader landing each chunk's `X`
and `R`, the execute task running the chunk's passes, the store sending `Y`.
`waveflow.vecunit.report.job_bounds` gives each from the program: the loader and the store at a word a
cycle -- or a value a cycle where a block's columns are not whole lane groups (`X`, one column, always;
`R` at one antenna) -- the execute task from its calibrated timing (filed at 2025.1). The **input floor** is the frame's
input words at a word a cycle. Four frames of four chunks a workload, each row the interval between
consecutive statuses:

| Build | Antennas | RTL, cycles a frame | input floor | loader | store | execute | binds |
|---|---|---|---|---|---|---|---|
| `VLMAX 64`, `L 4`, 64-bit | 8 | **1,464** (1.27×) | 1,152 | 1,304 | 1,028 | 1,508 | execute |
| `VLMAX 64`, `L 4`, 64-bit | 4 | **1,148** | 640 | 792 | 516 | 1,186 | execute |
| `VLMAX 64`, `L 4`, 64-bit | 1 | **1,148** | 256 | 536 | 260 | 1,186 | execute |
| `VLMAX 64`, `L 4`, 128-bit | 8 | **1,464** | 576 | 792 | 516 | 1,508 | execute |
| `VLMAX 64`, `L 4`, 128-bit | 1 | **1,148** | 128 | 536 | 260 | 1,186 | execute |
| `VLMAX 16`, `L 4`, 64-bit | 8 | **888** (3.08×) | 288 | 344 | 260 | 937 | execute |
| `VLMAX 256`, `L 4`, 64-bit | 8 | **5,167** (1.12×) | 4,608 | 5,144 | 4,100 | 3,791 | **load** |
| `VLMAX 64`, `L 8`, 64-bit | 8 | **1,327** (1.15×) | 1,152 | 1,304 | 1,028 | 1,186 | **load** |

(The execute column is the calibrated timing's, which overcharges this build's chunks by 3 to 6 %:
see [below](#pysim-against-the-rtl). A frame is `4·VLMAX` samples: 64 at `VLMAX 16`, 1,024 at 256.)

- **Short chunks pay the execute task's fixed costs.** A chunk is a few passes and two runs whatever its
  length, so at `VLMAX 16` the frame takes 13.9 cycles a sample against 5.7 at 64 and 5.05 at 256 (the
  floor is 4.5 words a sample at eight antennas).
- **Long chunks reach the loader.** At `VLMAX 256` the passes' fixed costs spread over 256 elements, the
  execute task falls behind the loader, and the frame runs at 1.12 times its input floor. What is left
  is the single-column `X`, which lands a value a cycle (256 cycles a chunk for its 128 words).
- **Four lanes or eight.** At `L 8` eight antennas are one column group, so a chunk is one pass
  instead of two, and the loader binds at 1.15 times the floor, for 101 DSP against 53.
- **Fewer antennas** take one column group from four down: the same 1,148 cycles at four antennas as at
  one. At one, `R` lands a value a cycle as `X` does, and the loader's 536 cycles a frame are still
  under the execute task's.
- **128-bit words change nothing** while the execute task binds: the same 1,464 and 1,148 cycles.

**The trace agrees.** `report.trace_report` on two traced runs: at `VLMAX 64` the loader waits for the
execute task to free an `X` block on 11 of 16 commits, and the input port is busy 81% of the run -- the
execute task binds, the loader close behind. At `VLMAX 256` the loader never waits on the execute task
and the port is busy 89%: the loader binds.

## Resources

csynth's estimate of the whole unit (Vitis HLS 2025.1):

| Build | LUT | FF | DSP | BRAM18K |
|---|---|---|---|---|
| `VLMAX 64`, `L 4`, 64-bit | 71,809 | 45,053 | 53 | 40 |
| `VLMAX 64`, `L 4`, 128-bit | 71,718 | 45,000 | 53 | 40 |
| `VLMAX 16`, `L 4`, 64-bit | 71,765 | 46,132 | 53 | 24 |
| `VLMAX 256`, `L 4`, 64-bit | 71,991 | 45,557 | 53 | 40 |
| `VLMAX 64`, `L 8`, 64-bit | 116,238 | 76,412 | 101 | 77 |

`VLMAX` costs memory only: 24 BRAM18K at 16, 40 at 64 and at 256.

## pysim against the RTL

pysim's frames, charged from the timing filed at 2025.1, run **3.1 to 5.6% long** where the execute
task binds (1,509 cycles against 1,464 at `VLMAX 64`, 938 against 888 at `VLMAX 16`) and **10.6 to
10.8% short** where the loader binds (1,187 against 1,327 at `L 8`, 4,608 against 5,167 at `VLMAX
256`) -- under the loader's bound in the table above. The timing was calibrated on the CG build, whose
execute task is scheduled differently: the decode loop (`RUNG`) is the same, II 14 and depth 24, but
`DELIVER` runs at II 3, depth 7 there against II 1, depth 3 here (csynth 2025.1). Stage 9d fitted a
timing that takes each build's loop schedule from csynth and scored it on held-out runs (at the
earlier tool version): it did worse on these frames, because the matched filter's frames, close to
the loader's bound, were not in its fit. pysim keeps Stage 7's form; the guide's
[calibration section](../../guide/dsp/vecunit/timing.md#the-calibrated-models) has both scores and the next
step.
