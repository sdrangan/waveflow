---
title: Systolic array timing
parent: Systolic Matrix Multiply
grand_parent: DSP Blocks
nav_order: 2
audience: python
api: [SystolicUnit, SystolicCore, TimedPhase, harness, add_calib_steps, ModuleStore]
summary: "Where the systolic unit's timing is measured and what it is: the unit in its own harness, fed straight from streams, at RTL; the core's timed phases (copy X, copy B, sweep the tiles, and two short gaps), each a span between two events on the core's own channels -- a FIFO read and the stream-of-blocks lock releases, read off the trace by name; the calibration that fits them (32 jobs, 8 held out, Vitis 2025.1: the copies a lane group a cycle exactly, the sweep 8.3+18.2·tiles+1.0·steps to within a cycle); the fix that made the unit pipeline -- a 2-deep reply FIFO had serialized load and store, 821 cycles a job, now 390; and what is not modelled yet."
---

# Timing

The [architecture](architecture.md) says what the unit does each cycle. This page says how its time is
measured, what was measured, and how pysim takes it. All numbers: Vitis HLS / Vivado xsim **2025.1**,
`xczu48dr-ffvg1517-2-e`, 4 ns, the centre configuration (`Mmax 8, Kmax 8, Nmax 32, R 4, C 8, L 4`,
`form 4`, 12-bit operands, 64-bit words).

## Where the timing is measured

The unit is stream-only, so it is measured **alone, fed straight from streams**, with no memory,
no `m_axi` and no bus between it and the measurement: its
[harness](../../flows/units_harness.md), `harness(unit)`, puts a stream driver on `s_in` and a sink on
`s_out`, each with its RTL twin, and runs the same scenario in pysim and under XSI
([`tests/linalg/_systolic_harness.py`](../../../../tests/linalg/_systolic_harness.py)). Two kinds of
record come out of the RTL run:

| Record | Where | What it holds |
|---|---|---|
| the boundary | `<work>/xsi_run/vectors/s_in_acc/cycles.bin`, `.../s_out/cycles.bin` | the cycle each input word was accepted and each output word arrived ([recordings](../../flows/units_recordings.md)) |
| the inside | `<work>/<top>_trace.vcd` | a level-1 dump of the unit's own top: its inter-task channels' handshake wires, which Vitis lifts to the top scope |

The inside is read **by name**: the unit's top's `trace_manifest()` names each channel's nets, and
[`load_trace`](../../../../waveflow/utils/trace.py) binds them to the VCD at the kernel's scope. A
stream-of-blocks channel's events are its lock handshakes:

| Event | RTL net | pysim |
|---|---|---|
| a block taken by its writer, by its reader | the writer's / reader's first `<inst>_<param>_ce0` after its previous release | `acquire_write` / `acquire_read` returns |
| a block committed by its writer | `<writer>_<param>_write` | `commit_write` |
| a block freed by its reader | `<reader>_<param>_read` | `release_read` |
| a command word read from a FIFO | `<reader>_<channel>_read` with `<channel>_empty_n` | the endpoint's transfer |

The pins are named after the task's **parameter** for a block channel (`..._U0_a_blk_read`) and after
the channel for a FIFO (`..._U0_ccmd_read`); the manifest knows both.

## The core's timed phases

The core is the unit's pace (the [schedule](architecture.md#the-schedule-measured)), so its time is
declared as [timed phases](../../timing_model/timed_phases.md) -- spans between events on its own
channels ([`waveflow/linalg/systolic.py`](../../../../waveflow/linalg/systolic.py),
`SystolicCore.__post_init__`):

| Phase | From | To | Features |
|---|---|---|---|
| `start` | the command read (`cmd_in.last`) | `X`'s block first touched (`a_blk.first`) | -- |
| `copy_x` | `X`'s block first touched | `X`'s block freed (`a_blk.last`) | `ag`: `X`'s lane groups, `⌈m·k/L⌉` |
| `to_b` | `X`'s block freed | the first `B`'s block first touched (`b_blk.first`) | -- |
| `copy_b` | a `B` block first touched | that block freed (`b_blk.last`) | `bg`: `B`'s lane groups, `k·n/L` |
| `sweep` | `B`'s block freed | `C` committed (`c_blk.last`) | `tiles`, and `steps` = `tiles·(k+R+C−2)` |

pysim's core waits each phase's predicted time (`phase.timeout(...)`), so pysim and the RTL time the
same intervals. **A copy is measured from its block's acquire**, never from an earlier event: when the
core waits for a block -- in the MIMO-CG detector the next `B` is the vector core's output -- a span
from an earlier event would hide the copy inside the wait. A tile's output groups (`R·C/L`) are fixed
by the configuration, so they are part of the per-tile cost, not a feature of their own.

**A held release.** The block buffer lets its writer fill the next block while the reader holds the
last one, but **commit** it only after the reader frees it: in the RTL, `i_full_n` falls as soon as one
block is committed and not yet freed, and a commit waits for it. So the core's `sweep` ends late when
the store is still writing the previous reply -- a short job after a long one. Such a row is the
environment holding the core, not the core's own time: the trace reader marks it held (`i_full_n` low
the cycle before the commit) and the fit leaves it out. pysim's block buffer does not have this
coupling yet ([limits](#limits)).

## The calibration

`add_calib_steps` runs the unit's calibration workload, `workload("calib")`, traced at RTL, joins the
trace's times with pysim's pairing and fits each span
([Calibrating in a harness](../../flows/units_calibration.md)). The workload is 32 requests: every row
count and column count the configuration allows (`m` 4, 8; `n` 8 to 32), at the smallest and largest
`k` (1 and 8), `MUL` and `MUL_AH`; jobs with 3 or 6 tiles are held out. The fit, filed under the core's
identity in a record store at `waveflow/linalg/calib/`:

| Phase | Fit (cycles) | Rows | Residual, mean (worst) |
|---|---|---|---|
| `start` | `5` | 32 | 0 (0) |
| `copy_x` | `1 + ag` | 32 | 0 (0) |
| `to_b` | `3` | 32 | 0 (0) |
| `copy_b` | `1 + bg` | 32 | 0 (0) |
| `sweep` | `8.3 + 18.2·tiles + 1.0·steps` | 23 fitted, 8 held out, 1 held | fitted 0.76 (1.30); **held out 0.68 (0.92)** |

The copies are exact: a lane group a cycle. The sweep is a cycle a step plus 18 a tile -- its 8 output
groups and the sweep pipeline's fill and drain -- and the ±1 left over is the two cycles the core
spends between row tiles. For the job of the main page's example the phases sum to
`5 + 17 + 3 + 65 + 298 = 388`, and the core reads the next command 2 cycles after committing `C`: the
390 measured. To repeat it:

```bash
python -m tests.linalg._systolic_harness --root C:/wfsc/sh --through calib_phases
```

Build from a short root: the generated task names make csynth's report paths long, and past Windows'
260-character limit csynth fails with no error message.

## The fix

The unit's tasks are joined by ping-pong blocks, so a job's load should overlap the previous job's
compute. Until 2026-10 it did not. Fed from streams, four back-to-back `8×8×32` jobs took **821
cycles** each: load (163 words), compute (390) and store (128 words) one after the other.

The trace showed why. The store keeps a reply header for its whole firing -- it writes the header, then
`C` -- and the receiver hands it the next header through the `reply` channel, which had the HLS default
depth of **2** words. A reply header is 3 words at 64 bits, so the receiver blocked on the next
header's last word, never forwarded the next payload, and job *n+1*'s load waited for job *n*'s store.
The block buffers were never the limit.

The channel now holds two headers (`2 · header_words`; the same in `CgVectorUnit`). The same four jobs
take **390 cycles** each -- the core's own work -- with the next job's load and the previous job's
store running while the core computes. `tests/linalg/test_systolic_harness.py` gates it at RTL.

**Fed from memory** -- through the in-band `MemRStream` and `MemWStream` of
`tests/linalg/_unit_bench.py`, sharing one generated top -- the same four jobs also take **390 cycles**
each (replies at 759, 1,149, 1,539, 1,929): in the steady state the memory side adds nothing. The ten
mixed jobs of `test_systolic_unit.py`'s centre run end at cycle 1,690 instead of 2,237.

So the packaged cycle model (`cost.message_model()`, fitted with Vitis 2024.1 on the unit before the
fix) measured the serialization: it was a sum of compute and transfer terms, about 720 cycles for this
job, and explained the sum by the generated top's `m_axi` pointer FIFOs. The pointer FIFOs do couple
how often the memory reader and writer fire, but they did not cause the sum. That model describes the
unit as it was.

## Limits

- **One configuration calibrated.** The centre is calibrated; any other configuration's core runs on
  the seed until `add_calib_steps` is run for it (its identity finds no record).
- **pysim's block buffer is looser than the RTL's.** pysim lets a writer commit a second block before
  the reader frees the first; the RTL does not ([above](#the-cores-timed-phases)). pysim is optimistic
  when the store is slower than the core (a short job after a long one).
- **Only the core is timed by phases.** The receiver, the loader and the store move a word a cycle in
  pysim, and the loader's transposing load (`MUL_AH`) takes its per-value cost from the 2024.1 cycle
  model. They overlap the core in the steady state, so the interval is the core's, but a job's
  latency -- request in to reply out -- is not calibrated.
- **Stalled consumers.** Every run drains `s_out` with an always-ready sink.
