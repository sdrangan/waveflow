# System spec: `hist_pipe`, generate, filter, histogram into shared memory

Frame: `bus_system` -- read [frame.md](../frame.md) first; this file adds only
what is specific to `hist_pipe`.

## 1. Overview

Three free-running kernels and an on-chip memory on one crossbar:

1. **gen** draws `n` pseudo-random uint16 values from a 32-bit xorshift
   (`x ^= x << 13; x ^= x >> 17; x ^= x << 5`, the value is the low 16 bits)
   seeded by the job;
2. **filter** keeps the values in `[lo, hi]` and drops the rest;
3. **hist** counts the kept values into 16 bins of equal width over
   `[lo, hi]` (bin `= ((v - lo) * 16) // (hi - lo + 1)`), writes the 16
   uint32 bins to the shared memory at the address the job names, and
   answers the host.

gen writes filter's input queue **over the bus**, holding credit for it;
filter streams to hist directly.

## 2. Messages

- **Command** (host -> gen): `tx_id`, `n` (1..4096), `seed` (uint32,
  nonzero), `lo`, `hi` (uint16, `lo <= hi`), `addr` (where the bins go).
  The command travels with the data through all three kernels.
- **Response** (hist -> host): `tx_id`, `n`, `n_kept` (uint16), once the bins
  are in memory.

## 3. The host

Two jobs in flight, bounded by a semaphore; a writer thread and a reader
thread; it waits on interrupts only.  For each response it reads the job's
16 bins back from memory and checks them, with `n_kept`, against the golden
model.  Each job in flight has its own memory region.

## 4. Scenario

Eight jobs from a fixed seed, including `lo == hi`, a range that keeps
nothing, and `n = 4096`.

## 5. Acceptance

- Golden model with worked examples; pysim bit-exact.
- The system DAG at RTL: traces identical to pysim, zero polls, cycles
  recorded and pysim within 5%.
- `results/report.md`: the gates, the credit window on the gen -> filter
  link and its sizing, and the memory regions.
