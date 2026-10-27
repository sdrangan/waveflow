---
title: The matched filter on the vector unit, timing at RTL
parent: A matched filter as a vector program
nav_order: 1
summary: "What the RTL measured (Vitis HLS / Vivado xsim 2024.1, xczu48dr, 4 ns) for the matched filter on the vector unit, across antenna counts, chunk lengths (VLMAX 16, 64, 256), word widths and lanes: the execute task binds at short chunks (2.9 times the input floor at VLMAX 16, 1.22 at 64), the loader at long ones (1.12 at VLMAX 256) and at L 8 (1.15); 128-bit words change nothing while the execute task binds. What a trace shows, and pysim's known 7.5 to 13% overcharge a chunk."
---

# Timing at RTL

Every number on this page is an RTL run of the unit in its harness, bit-exact against pysim, from
`examples/vecunit_mf/mf_measure.py` (Vitis HLS / Vivado xsim 2024.1, `xczu48dr` at 4 ns, csynth's
estimated clock 3.12 to 3.14 ns on every build):

```bash
python -m examples.vecunit_mf.mf_measure --root /tmp/vu_mf_m     # -> measured.json, 11 runs, 5 builds
```

## The frames against their bounds

A frame's time is set by the slowest of the unit's three tasks: the loader landing each chunk's `X`
and `R`, the execute task running the chunk's passes, the store sending `Y`.
`waveflow.vecunit.report.job_bounds` gives each from the program: the loader and the store at a word a
cycle -- or a value a cycle where a block's columns are not whole lane groups (`X`, one column, always;
`R` at one antenna) -- the execute task from its calibrated timing. The **input floor** is the frame's
input words at a word a cycle. Four frames of four chunks a workload, each row the interval between
consecutive statuses:

| Build | Antennas | RTL, cycles a frame | input floor | loader | store | execute | binds |
|---|---|---|---|---|---|---|---|
| `VLMAX 64`, `L 4`, 64-bit | 8 | **1,408** (1.22×) | 1,152 | 1,304 | 1,028 | 1,512 | execute |
| `VLMAX 64`, `L 4`, 64-bit | 4 | **1,092** | 640 | 792 | 516 | 1,195 | execute |
| `VLMAX 64`, `L 4`, 64-bit | 1 | **1,092** | 256 | 536 | 260 | 1,195 | execute |
| `VLMAX 64`, `L 4`, 128-bit | 8 | **1,408** | 576 | 792 | 516 | 1,512 | execute |
| `VLMAX 64`, `L 4`, 128-bit | 1 | **1,092** | 128 | 536 | 260 | 1,195 | execute |
| `VLMAX 16`, `L 4`, 64-bit | 8 | **832** (2.89×) | 288 | 344 | 260 | 941 | execute |
| `VLMAX 256`, `L 4`, 64-bit | 8 | **5,167** (1.12×) | 4,608 | 5,144 | 4,100 | 3,797 | **load** |
| `VLMAX 64`, `L 8`, 64-bit | 8 | **1,327** (1.15×) | 1,152 | 1,304 | 1,028 | 1,195 | **load** |

(The execute column is the calibrated timing's, which overcharges this build's chunks: see
[below](#pysim-against-the-rtl). A frame is `4·VLMAX` samples: 64 at `VLMAX 16`, 1,024 at 256.)

- **Short chunks pay the execute task's fixed costs.** A chunk is a few passes and two runs whatever its
  length, so at `VLMAX 16` the frame takes 13 cycles a sample against 5.5 at 64 and 5.05 at 256 (the
  floor is 4.5 words a sample at eight antennas).
- **Long chunks reach the loader.** At `VLMAX 256` the passes' fixed costs spread over 256 elements, the
  execute task falls behind the loader, and the frame runs at 1.12 times its input floor. What is left
  is the single-column `X`, which lands a value a cycle (256 cycles a chunk for its 128 words).
- **Four lanes or eight.** At `L 8` eight antennas are one column group, so a chunk is one pass
  instead of two, and the loader binds at 1.15 times the floor, for 101 DSP against 53.
- **Fewer antennas** take one column group from four down: the same 1,092 cycles at four antennas as at
  one. At one, `R` lands a value a cycle as `X` does, and the loader's 536 cycles a frame are still
  under the execute task's.
- **128-bit words change nothing** while the execute task binds: the same 1,408 and 1,092 cycles.

**The trace agrees.** `report.trace_report` on two traced runs: at `VLMAX 64` the loader waits for the
execute task to free an `X` block on 9 of 16 commits, and the input port is busy 84% of the run -- the
execute task binds, the loader close behind. At `VLMAX 256` the loader never waits on the execute task
and the port is busy 89%: the loader binds.

## Resources

csynth's estimate of the whole unit:

| Build | LUT | FF | DSP | BRAM18K |
|---|---|---|---|---|
| `VLMAX 64`, `L 4`, 64-bit | 74,506 | 48,342 | 53 | 32 |
| `VLMAX 64`, `L 4`, 128-bit | 74,450 | 48,853 | 53 | 32 |
| `VLMAX 16`, `L 4`, 64-bit | 74,334 | 48,224 | 53 | 32 |
| `VLMAX 256`, `L 4`, 64-bit | 74,688 | 48,361 | 53 | 44 |
| `VLMAX 64`, `L 8`, 64-bit | 124,052 | 81,809 | 101 | 64 |

`VLMAX` costs memory only: four times the elements, 12 more BRAM18K.

## pysim against the RTL

pysim's frames run **7.5 to 13% long** on these builds (1,513 cycles against 1,408 at `VLMAX 64`).
The timing was calibrated on the CG build, whose execute-task decode loop is deeper (csynth: `RUNG`
II 13, depth 23 against 12 and 14 here; `DELIVER` depth 7 against 6). Stage 9d fitted a timing that
takes each build's loop schedule from csynth and scored it on held-out runs: it did worse on these
frames (17 to 22%, though better at `L 8`), because the matched filter's frames, close to the
loader's bound, were not in its fit. pysim keeps the Stage 7 timing; the guide's
[calibration section](../../guide/dsp/vecunit/timing.md#the-calibrated-models) has both scores and the next
step.
