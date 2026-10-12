# Two kernels on a bus: scale, then sum

Build a small system of two hardware kernels that talk to each other, and to a
host, over one shared AXI memory-mapped bus.

## Function

A **job** is a gain `g` and `n` signed 16-bit samples `x[0..n-1]`.

- The **scale** kernel computes, for each sample,
  `p[i] = sat16((x[i] * g + 128) >> 8)`: `g` is Q8.8 (the real gain is
  `g / 256`), `>>` is an arithmetic shift (so the rounding is half up), and
  `sat16` saturates to the int16 range.
- The **sum** kernel computes `s = p[0] + ... + p[n-1]` exactly, as an int32
  (it cannot overflow for `n <= 1024`).

## Architecture

- One AXI memory-mapped bus (a crossbar), 64 bits wide.  On it: the host, the
  scale kernel and the sum kernel.  The kernels are free-running: once reset
  is released they wait for work; there is no per-job start signal.
- The **host** writes each job to the scale kernel through a queue on the
  bus: a command word holding a job id `tx_id` (uint16), `n` (uint16,
  1..1024) and `g` (int16), then the `n` samples, packed four per 64-bit word.
- **Scale writes its products into the sum kernel's input queue over the same
  bus**, as the bus master of that link: first a word carrying `tx_id` and
  `n`, then the products, four per word.  Scale must **never overrun** sum's
  queue, and must **never hold the bus waiting** for room in it -- other
  masters must keep their access while sum is busy.
- Sum answers through a response queue the host reads over the bus: one word
  holding `tx_id`, `n` and `s`.
- The host **does not poll**: it waits on interrupts from the kernels (room in
  scale's queue, a response in sum's).  It runs one job at a time, and checks
  each response's `tx_id`, `n` and `s`.

## What to deliver

1. **A Python model** of the function, bit-exact, with worked examples
   computed by hand (including rounding and saturation cases).
2. **A scenario**: eight jobs from a fixed seed, with lengths 1, 3, 4, 5 and
   1024 among them, gains including 0, 256 and -256, and samples at the
   int16 extremes; and its expected responses from the Python model.
3. **The two kernels** in Vitis HLS C++, synthesized (C synthesis must
   complete).
4. **An RTL simulation of the whole system** -- the host, the bus and both
   kernels' synthesized RTL -- running the scenario, with every response
   bit-exact against the expected ones.  The host in this simulation may be a
   testbench, but it must reach the kernels only through the bus and the
   interrupts.
5. **`report.md`**: the result of each check and where it came from; how
   the sum kernel's queue is kept from overflowing without stalling the bus,
   and how big you made it; the address map; and every assumption you made
   where this spec is silent.

Timing calibration, other bus widths and parameter validation are out of
scope.
