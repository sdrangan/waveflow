# Accelerator spec: `gather`, a permutation by an index vector

Frame: `freerun_pipeline` -- read [frame.md](../frame.md) first; this file
adds only what is specific to `gather`.

## 1. Overview

For each command, read a vector `x` of `n` uint32 values and an index vector
`p` of `n` uint16 indices, and write `y[i] = x[p[i]]`.  The gather happens on
chip: `x` is held in a block the compute stage reads at random; `p` is read
in order.

## 2. Messages

- **Command**: `tx_id` (uint16), `n` (uint16, 1..1024), `x_addr`, `p_addr`,
  `y_addr` (byte addresses, 8-byte aligned).
- **Error**: an index `p[i] >= n` is not an error: it reads `x[p[i] mod n]`.
  State this in the golden model and test it.
- **Response**, once `y` is written: `tx_id`, `n`.

## 3. Parallelism

A 64-bit memory bus: two `x` or `y` values, four `p` indices per word.

## 4. Scenario

Twelve commands from a fixed seed: the identity, a reversal, a random
permutation, a vector with repeated indices, indices at or above `n`, and
`n` of 1 and 1024.

## 5. Acceptance

- Golden model with worked examples; pysim bit-exact.
- The generated XSI testbench bit-exact at RTL; the cycle count recorded as
  an exact gate; pysim's per-job period compared against it, and the gap
  reported.
- `results/report.md` as in the frame.
