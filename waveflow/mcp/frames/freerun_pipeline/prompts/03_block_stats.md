# Accelerator spec: `block_stats`, min, max and sum per block

Frame: `freerun_pipeline` -- read [frame.md](../frame.md) first; this file
adds only what is specific to `block_stats`.

## 1. Overview

For each command, read `n` signed 16-bit samples from memory, split them into
consecutive blocks of `b` samples (the last block may be shorter), and write
one record per block to memory: the block's minimum, maximum and sum.

## 2. Messages

- **Command**: `tx_id` (uint16), `n` (uint32, 1..8192), `b` (uint16,
  1..1024), `src`, `dst` (byte addresses, 8-byte aligned).
- **Record** (written to `dst`, one per block, in order): `min` (int16),
  `max` (int16), `sum` (int32) -- one 64-bit word per record.
- **Response**, once the last record is written: `tx_id`, `n_blocks`.

## 3. The stage with state

The stats stage keeps its running minimum, maximum and sum across firings,
until a block ends.  Declare that state (`add_state`); a block's boundary
can fall in the middle of a memory word.

## 4. Parallelism

A 64-bit memory bus: four samples per word in, one record per word out.

## 5. Scenario

Ten commands from a fixed seed: `b` of 1, 3, 4 and larger than `n`; `n` not
a multiple of `b`; samples at the int16 extremes.

## 6. Acceptance

- Golden model with worked examples; pysim bit-exact.
- The generated XSI testbench bit-exact at RTL; the cycle count recorded as
  an exact gate; pysim's per-job period compared against it.
- `results/report.md` as in the frame.
