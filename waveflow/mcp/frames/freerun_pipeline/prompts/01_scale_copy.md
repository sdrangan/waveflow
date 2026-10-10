# Accelerator spec: `scale_copy`, read, scale, write

Frame: `freerun_pipeline` -- read [frame.md](../frame.md) first; this file
adds only what is specific to `scale_copy`.

## 1. Overview

For each command, read `n` signed 16-bit samples from memory, multiply each
by the command's gain, and write the results to memory: a memory copy with
one compute stage between the read and the write.

## 2. Messages

- **Command** (on the command stream): `tx_id` (uint16), `n` (uint32,
  1..4096), `src` and `dst` (byte addresses, 8-byte aligned, regions that do
  not overlap), `gain` (int16, Q8.8: the real gain is `gain / 256`).
- **The work**: `y[i] = sat16((x[i] * gain + 128) >> 8)` -- round half up,
  arithmetic shift, saturated to int16.
- **Response** (on the done stream), sent once the last word of `y` is
  written: `tx_id`, `n`.

## 3. Parallelism

A 64-bit memory bus: four samples per word.  The scale stage handles a word
per firing, its four lanes unrolled.  An `n` that is not a multiple of four
leaves the rest of the last word as it was in memory.

## 4. Scenario

Sixteen commands from a fixed seed: `n` of 1, 3, 4, 5 and 4096 among them;
gains of 0, 256, -256 and the int16 extremes; samples including the int16
extremes.

## 5. Acceptance

- Golden model with worked examples; pysim bit-exact on the output memory
  image and the responses.
- The generated XSI testbench bit-exact at RTL; the cycle count recorded as
  an exact gate; pysim's per-job period within 3% of the RTL's.
- `results/report.md`: each gate, its number, and where it came from.
