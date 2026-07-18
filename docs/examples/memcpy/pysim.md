---
title: Python Simulation
parent: Memory Copy
nav_order: 4
---

# Python simulation

The pysim rung runs the whole design — DUT, driver, sink, memory — as a SimPy discrete-event
simulation. No toolchain, no RTL, seconds rather than minutes. It is where the design is validated
first, and it catches most functional mistakes long before any C++ exists.

It runs the *same graph* the RTL testbench is generated from, so what you check here is not a
simplified model of the design; it is the design, executed by a different backend.

## Running it

```bash
python examples/mem_copy/mem_copy_sim.py
```

```
[copy] src=16 dst=512 n=128 ok=True
[copy] jobs=1 done_tokens=1 all_ok=True
[copy] src=16 dst=600 n=128 ok=True
[copy] src=200 dst=900 n=64 ok=True
[copy] jobs=2 done_tokens=2 all_ok=True
mem_copy pysim golden: PASSED
```

Two scenarios: one copy, then two back-to-back copies at distinct offsets. The driver is the entry
point for your own:

```python
from examples.mem_copy.mem_copy_sim import run_copy

dut = run_copy(jobs=((16, 600, 128), (200, 900, 64)), mem_dwidth=64)
```

Each job is a `(src_off, dst_off, n_words)` triple in **element/word coordinates**. `run_copy` builds
the testbench, runs it, checks it, and hands back the DUT so you can inspect what happened.

## The scenario is stated once

Building `MemCopyTB` *is* building the scenario. From the `jobs` list it derives everything:

| what | how |
|---|---|
| the commands | one `CopyCmd` per job, serialized and written as a burst bundle the `StreamDriver` plays |
| the source data | a per-job pattern written straight into the arena |
| the expectation | the same pattern, kept as `expected` for the comparison |

The XSI vectors are then *serialized from that testbench* rather than recomputed —
`write_mem_copy_xsi_bundles` takes the commands from `driver.bursts` and the arena and result from
`mem_image` / `golden_image`. So the pysim run and the RTL run cannot start from different bytes:
there is one scenario with two serializations, not two scenarios that have to agree.

Note the asymmetry, because it is easy to misread: the **commands** genuinely round-trip through a
bundle even in pysim (written to a temp directory, read eagerly at construction), while the **memory**
is seeded in-process and the bundle is derived from that seed for the C++ side.

## The lifecycle

`sim.run_sim()` drives every registered `SimObj` through three phases:

1. `pre_sim()` on all objects, in registration order;
2. each object's `run_proc()` scheduled as a SimPy process — objects returning `None` are skipped
   (a passive participant like the memory has no loop of its own);
3. `env.run()` until no events remain, then `post_sim()` on all objects.

This is the same shape as the C++ `XsiSimObj` lifecycle at the RTL rung, which is not a coincidence —
[the BFM models](../../guide/build/bfm.md) were built to mirror it.

Two traps worth knowing:

- **Never call `sim.add_obj()` yourself.** `SimObj.__init__` already registers with the active
  simulation; calling it again double-registers the object, and it will be stepped twice.
- **`elaborate()` is not pysim.** Elaboration builds a simulation-free graph for code generation. If
  you want behavior, build the testbench with a real `Simulation` and call `run_sim()`.

## Validating the operation

The golden is a bit-exact comparison, per job, plus a completion count:

```python
got = mem._mem.read(dst * bpw, n)
assert np.array_equal(got, expected)          # every destination word
assert len(done_sink.words) == len(jobs)      # one MemComplete per job
```

Both matter. The region compare catches a wrong address, a short burst, or a dropped word. The token
count catches the failure the compare cannot see: a copy that happened but never reported, which at
the RTL rung would hang a host waiting on a completion that never arrives.

Because the driver never waits for a completion, multiple jobs **overlap** — the kernel is already
reading job *j+1* while still writing job *j*. That is the property the whole free-running design
exists for, so a testbench that issued one job and awaited it would pass while silently destroying the
thing under test.

## Measuring timing

pysim carries a timing model, so the run yields real numbers. Simulated time is in seconds; multiply
by the clock frequency for cycles:

```python
tb.sim.run_sim()
cycles = tb.sim.env.now * tb.clk.freq          # 100 MHz -> cycles
spans  = tb.dut.rstream.transfer_spans         # per-transfer durations, seconds
```

For the 16-job scenario the RTL gate uses (128 words per job):

| measure | value |
|---|---|
| one job, end to end | 159 cycles |
| 16 jobs, end to end | 2214 cycles |
| per-transfer span | read 137 cycles, write 140 cycles |

Those decompose exactly — `159 + 15 x 137 = 2214` — into a **fill latency** of 159 cycles and a
**steady-state period** of 137 cycles per job. And the period is the interesting part: a job reads for
137 and writes for 140, so a sequential design would need ~277 cycles per job. Getting ~137 means the
read and the write are overlapping — `max(read, write)`, not `read + write`. The pipeline works.

### How close is it to the RTL?

The same scenario at the [XSI rung](./testbench.md) reports per-job completion cycles, so the two are
directly comparable:

| | pysim | RTL (XSI) | pysim / RTL |
|---|---|---|---|
| fill (first completion) | 159 | 171 | 93% |
| steady-state period | 137 | 178 | 77% |
| total, 16 jobs | 2214 | 2835 | 78% |

**pysim gets the architecture right and the absolute numbers optimistic.** Both agree the design is
pipelined rather than sequential, both agree the period is dominated by one direction rather than the
sum, and both agree on the job count and ordering. But pysim under-counts the steady-state period by
roughly a quarter, because a transaction-level model does not reproduce every source of per-cycle
contention in the generated RTL.

So use each rung for what it is good at:

- **pysim** — correctness, overlap and structure, deadlock, job accounting. Fast enough to run on
  every edit.
- **RTL** — the number you would quote. The `-m xsi` gate asserts 2835 exactly, so a change that
  perturbs the schedule fails loudly.

Closing that ~22% gap is calibration work, and it is open: the timing model has parameters that can be
fit against cosim, and doing so for this design has not been done yet. Until then, treat a pysim cycle
count as a lower bound with the right shape, not as a prediction.

## Next

- [Testbench](./testbench.md) — the same graph at the RTL rung, and the generated XSI testbench.
- [Kernel codegen](./codegen.md) — how the graph becomes the `ap_ctrl_none` top.
