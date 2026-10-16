# Plan: interrupts for the memory-mapped queues, and no polling in the examples

> **Status (2026-10-03): BUILT, Stages 1-4** (branch `mm-host-endpoints`, unpushed). mm_fir has no
> polling, in pysim and at RTL: 520 / 529 cycles, 67 bus ops (was 768 / 783, 124). Section headings are
> cited from code -- do not rename them. Departures are in **Built** at the end.

## Why

Every wait a host does on the slave adaptor today is **polling**: read a queue's free space or
occupancy, sleep, read again (`plans/mm_adaptor_host_endpoints.md`, decision D3). It works and it never
stalls the bus, but it is not how a real driver waits, and it is not what the examples should teach.
A real host sleeps until the device **interrupts**.

There is no interrupt anywhere in the framework: `VitisRegMap` carries Vitis's `GIER` / `IER` / `ISR`
as storage only ("there is no interrupt line in the simulation"), and the regmap example tells its host
to "poll `ap_done` (or wait for an interrupt)".

## The design

### D1. `IrqIF` -- a level-sensitive interrupt line, in every backend

| | pysim | RTL | XSI |
|---|---|---|---|
| `IrqIF` | a level the source sets; the receiver waits on it as an event | a 1-bit wire | a pin the testbench host samples |

- `IrqIFSource.set(level)` -- instant, no simulated time (a wire).
- `IrqIFSink.level` and `IrqIFSink.wait_high()` -- returns at once if the line is high, otherwise when
  it rises. No polling: the sink is woken by the source's edge.
- **Level, not edge:** the source holds the line high while its condition is true, and the host clears
  it by removing the condition (draining the queue). A host that wakes late cannot miss an edge.

Enable / status / clear registers (Vitis's `GIER` / `IER` / `ISR`) are not part of this plan: a level
line per condition is enough to remove the polling.

### D2. Each queue view drives an interrupt at a threshold the host writes

| view | interrupt is high while | threshold written at |
|---|---|---|
| queue out | `occupancy >= threshold` -- that many words are ready | the status half (`window / 2`) |
| queue in | `vacancy >= threshold` -- that many free slots | the **upper half** (`window / 2`) |

- **Threshold 0 disables the interrupt** and is the reset value, so a design that writes no threshold
  sees no change: reads, pops, pushes and every recorded cycle count stay as they are.
- **Queue in reserves its upper half for control.** Pushes go to the lower half; a write to the upper
  half sets the threshold and pushes nothing. (No existing writer used the upper half: an AXI4 burst is
  at most 256 beats, 2 KB at 64 bits, and every gate pushes from the window's base.)
- Reads are unchanged: queue in returns the vacancy anywhere, queue out pops in the lower half and
  returns the occupancy in the upper.

### D3. The endpoints wait on the interrupt instead of polling

`BoundMemSlaveAdaptor.stream_master(name, irq=sink)` / `stream_slave(name, irq=sink)`: given the
host's end of the view's interrupt line, the endpoint never reads a count.

- **Queue out, `get(n)`:** set the threshold to `n` (one bus write, only when it changes), wait for the
  interrupt, pop `n`. The interrupt *means* `n` words are there, so there is nothing to check.
- **Queue in, `write(n words)`:** the endpoint keeps a lower bound on the free space. When it is below
  `n`, it sets the threshold to `max(n, depth / 2)` (once, normally), waits for the interrupt, and
  takes that as the new lower bound. Then it writes and subtracts `n`. One threshold write for the
  whole run, one wait per half-queue of data.
- Without `irq=`, the endpoints keep D3's polling, as a fallback -- not used by any example.

### D4. mm_fir with no polling anywhere

- The host's writer waits on queue in's interrupt; its reader waits on queue out's and the response
  FIFO's.
- **The kernel publishes its status before it writes the packet's response,** so a host holding the
  last response reads the final status once -- no "until `nsamp` reaches N" loop. (HLS body: the RESP
  state waits until the status has gone out.)
- The XSI testbench's host watches the three interrupt pins (`xsi_mm_host.h`: an `IrqPin` sampled each
  cycle; the endpoints take one). The RTL top exposes each view's `irq`.

### D5. Also without polling: the slave guide's runnable example

Its host waits on queue out's interrupt for its four results. The guide recommends the interrupt mode
and calls polling a fallback.

## Stages

1. **pysim:** `IrqIF`; the views' thresholds and interrupts; the endpoints' interrupt mode; tests
   (wait wakes on the edge; a level already high returns at once; threshold 0 = never; a host with the
   interrupt issues no count reads at all -- counted).
2. **RTL:** `mm_queue_in.v` / `mm_queue_out.v` threshold register + `irq`; `mm_adaptor_gen` wires each
   queue view's `irq` out; every existing adaptor XSI gate unchanged (thresholds stay 0).
3. **mm_fir:** pysim host and kernel (status before response) and the HLS body; C++ `IrqPin` and the
   endpoints' interrupt mode; the testbench host; re-measure. All three wirings and both topologies.
4. **Docs:** the views, the endpoints, mm_fir, the slave guide's example.

Later, not in this plan: `ap_done` as an `IrqIF` for host-activated kernels (retires the regmap
examples' polling), and the two-kernel command/response example.

## Built

- As designed, with these notes:
- **The kernel's status-before-response cost a timing fix in HLS.** Requesting the status on the last
  sample's firing put `nsamp++` and the status packing in one cycle: estimated 13.3 ns against a 10 ns
  clock.  The RESP state now requests it in its own first firing and writes the response once it has
  gone out: 7.7 ns, II=1, latency 10, unchanged resources.
- **The slave guide's runnable example** waits on two interrupts (queue in, queue out), and its kernel
  publishes the status before the results -- the same rule as mm_fir's.
- **The C++ queue writer's interrupt mode refuses a packet longer than the queue** (pysim splits it in
  pieces); no example sends one.
- **Gates:** `test_mm_host.py` (the line; both views' thresholds; a writer+reader host in interrupt
  mode drains everything and reads no count), `test_mm_fir.py::test_the_host_never_polls` (pysim, both
  bus wirings), `test_mm_fir_xsi.py::test_mm_fir_rtl_host_never_polls` (RTL, parsed from the
  testbench's bus operations).  `WANT_XSI_GATES` 140 -> 142.  Every other adaptor XSI gate keeps its
  number (thresholds stay 0).
