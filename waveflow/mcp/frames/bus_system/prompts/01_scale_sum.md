# System spec: `scale_sum`, two kernels on a bus

Frame: `bus_system` -- read [frame.md](../frame.md) first; this file adds only
what is specific to `scale_sum`.

## 1. Overview

A host sends jobs of signed 16-bit samples.  A **scale** kernel multiplies
each sample by the job's gain and passes the products on; a **sum** kernel
accumulates them and answers the host with the job's sum.  The same two
kernels are built twice:

- **joined directly**: scale streams to sum on an on-chip link;
- **routed**: scale writes sum's input queue over the crossbar, holding
  credit for it.

Both wirings must produce identical responses.

## 2. Messages

- **Command** (host -> scale, through scale's command queue): `tx_id`
  (uint16), `n` (uint16, 1..1024), `gain` (int16, Q8.8: the real gain is
  `gain / 256`).  The samples follow in the same queue: `n` int16 values,
  packed two per 32-bit lane by the Waveflow array utilities.
- **Scale -> sum**: the command travels with the data.  Scale forwards
  `tx_id` and `n`, then `n` products `p = (x * gain + 128) >> 8`
  (round half up, arithmetic shift), saturated to int16.
- **Response** (sum -> host, through sum's response queue): `tx_id`, `n`,
  `sum` (int32, the exact sum of the `n` products -- it cannot overflow).

## 3. The host

Two threads -- one writes jobs, one reads responses -- with at most two jobs
in flight.  It waits on interrupts only: room in scale's command queue, a
response in sum's.  It checks every response's `tx_id` and `n` against the
job it sent.

## 4. Scenario

Twelve jobs from a fixed seed: lengths 1, 2, 3 and 1024 among them, gains
including 0, 256 (unity), -256 and the extremes of int16, samples including
the extremes of int16 (so saturation is exercised).

## 5. Acceptance

- Golden model with worked examples; pysim bit-exact in both wirings.
- The routed wiring through the system DAG at RTL: traces identical to
  pysim, zero polls, the cycle count recorded and pysim within 5%.
- `results/report.md`: each gate, its number, and where it came from; the
  credit window you chose for the routed link, and why.
