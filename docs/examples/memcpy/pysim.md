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

The example uses the standard build CLI; `pysim` is the default target:

```bash
python examples/mem_copy/mem_copy_build.py --through pysim
```

> **Works from any directory.** The examples are an installed package (`pip install -e ".[dev]"`), so
> `from examples.mem_copy.… import …` and the build CLI resolve wherever you run them — the repo root,
> the example directory, anywhere. No `PYTHONPATH` or `sys.path` juggling.

```
pysim:
    results\pysim.json
    RUNNING...
[pysim] 16 jobs, all bit-exact, 16 done tokens, end=2214 cycles
    PASSED
```

That runs the canonical 16-job scenario and writes `results/pysim.json` (correctness + timing). It is
the first `--through` target in the DAG (`--list-steps` shows `pysim → gen → csynth`); no toolchain,
seconds not minutes.

To drive your own scenario, call `run_copy` directly:

```python
from examples.mem_copy.mem_copy import CopyJob
from examples.mem_copy.mem_copy_sim import run_copy

dut = run_copy(jobs=(CopyJob(src_off=16, dst_off=600, n_words=128),
                     CopyJob(src_off=200, dst_off=900, n_words=64)), mem_dwidth=64)
```

Each job is a [`CopyJob`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/mem_copy.py)
— named fields in **word coordinates**, so you never have to remember an offset order (a bare
`(src, dst, n)` tuple is accepted too and coerced). `run_copy` builds the testbench, runs it, checks
it, and hands back the DUT so you can inspect what happened.

## What you write

The entire hand-written surface for this rung is one class:
`MemCopyTB(FreeRunComp)` — a composite (a `FreeRunComp` with sub-components) — in
[`mem_copy_sim.py`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/mem_copy_sim.py).
Everything it *uses* — the driver, the sink, the memory model, the interfaces, the run loop — is
framework. What you write is `__post_init__`: instantiate the participants around the DUT and wire
them. Walking through it, in order.

**Declare the knobs.** A testbench is a component, so its parameters are dataclass fields:

```python
@dataclass
class MemCopyTB(FreeRunComp):
    jobs: tuple = (CopyJob(src_off=16, dst_off=512, n_words=128),)   # word coordinates
    mem_dwidth: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
```

`jobs` is a tuple of [`CopyJob`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/mem_copy.py)s
— named fields, all in word coordinates. `__post_init__` coerces each (a bare `(src, dst, n)` tuple is
accepted too) into `self._jobs`.

**Build the memory.** One flat arena large enough for every source and destination region, wrapped in
a `MemComponent`. This is the participant behind *both* `m_axi` bundles:

```python
        self.arena_words = max(max(job.src_off, job.dst_off) + job.n_words
                               for job in self._jobs) + 16
        self.mem = MemComponent(name=f"{self.name}_mem", sim=self.sim, inline=False, clk=self.clk,
                                word_size=w, addr_size=32, nwords_tot=self.arena_words * 4)
        self.mem.alloc(self.arena_words)
```

**Seed the source, and remember what you expect.** This is the scenario. Fill each source region with
a per-job, full-width **seeded** pattern, and keep the array to check against later:

```python
        self.expected: list[np.ndarray] = []
        for j, job in enumerate(self._jobs):
            rng = np.random.default_rng(0xC0FFEE + j)             # seeded -> reproducible
            known = rng.integers(0, 1 << w, size=job.n_words, dtype=np.uint64)   # full width
            self.mem._mem.write(job.src_off * bpw, known)
            self.expected.append(known)
```

The seed matters twice over: it exercises **every** bit of the word (a low-magnitude ramp like
`arange(n)` would leave the top bits zero, so a bug dropping the high word half could pass unseen), and
it stays **reproducible** — a failing run replays exactly from `0xC0FFEE + j`.

**Instantiate the DUT** — the component under test, unchanged from how it is used anywhere else:

```python
        self.dut = MemCopy(name=f"{self.name}_copier", sim=self.sim, mem_dwidth=w)
```

**Make the command source.** The testbench owns the command schema: it builds one `CopyCmd` per job
and serializes them to raw stream words (kept as `cmd_words`). The `StreamDriver` is schema-blind — it
just plays a bundle, and it names the *path* it will read, not the data:

```python
        self.cmds = [CopyCmd(src_off=job.src_off, dst_off=job.dst_off, n_words=job.n_words, tx_id=j)
                     for j, job in enumerate(self._jobs)]
        self.cmd_words = [np.asarray(c.serialize(word_bw=w), dtype=np.uint64) for c in self.cmds]
        self.driver = StreamDriver(sim=self.sim, bitwidth=w, in_bundle="vectors/s_cmd")
```

`in_bundle` is the *one* path both backends read: the generated XSI harness emits
`s_cmd.in_bundle = "vectors/s_cmd";` and the C++ `AxisMaster` loads it in `pre_sim`; pysim's
`StreamDriver` loads the same path in *its* `pre_sim`. The bytes are written once, by
`write_scenario` (below) — no data lives on the driver at construction.

**Make the completion sink** — a `StreamSink` collects the `MemComplete` records off `s_done`:

```python
        self.done_sink = StreamSink(sim=self.sim, bitwidth=w, out_bundle="vectors/s_done")
```

**Register the participants**, in the order the code generator should walk them:

```python
        for c in (self.dut, self.driver, self.done_sink, self.mem):
            self.add_comp(c)
```

**Wire the graph.** Three interfaces, bound master-to-slave, exactly as sub-components are wired inside
a design. Two streams, and one crossbar carrying both `m_axi` bundles onto the single arena:

```python
        cmd_if = StreamIF(name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        cmd_if.bind(ep_name="master", endpoint=self.driver.stream_ep)   # driver -> DUT command in
        cmd_if.bind(ep_name="slave",  endpoint=self.dut.s_cmd)
        self.add_if(cmd_if)

        done_if = StreamIF(name=f"{self.name}_done_if", sim=self.sim, clk=self.clk, bitwidth=w)
        done_if.bind(ep_name="master", endpoint=self.dut.s_done)        # DUT completions -> sink
        done_if.bind(ep_name="slave",  endpoint=self.done_sink.stream_ep)
        self.add_if(done_if)

        xbar = AXIMMCrossBarIF(name=f"{self.name}_xbar", sim=self.sim, clk=self.clk,
                               nports_master=2, nports_slave=1, bitwidth=w)
        xbar.bind("master_0", self.dut.m_in)     # read bundle  (gmem0)
        xbar.bind("master_1", self.dut.m_out)    # write bundle (gmem1)
        xbar.bind("slave_0",  self.mem.s_mm)     # ... both onto the one arena
        self.add_if(xbar)
        assign_address_ranges([self.mem.s_mm], [(0, self.arena_words * bpw)])
```

`__post_init__` builds the **structure** — the participants and their wiring. It writes no scenario
data: the driver names a path, not bytes. The **scenario** is a separate step, `write_scenario`, which
materializes the vectors and points the participants at them:

```python
    def write_scenario(self, root) -> None:
        write_burst_bundle(self.cmd_words,      root / "vectors" / "s_cmd")
        write_burst_bundle([self.mem_image],    root / "vectors" / "mem_in")
        write_burst_bundle([self.golden_image], root / "vectors" / "golden")
        self.driver.root = root      # the driver resolves in_bundle against this in pre_sim
```

That structure/scenario split is why one class serves both backends cleanly: walking the graph
generates the XSI harness (it never looks at the scenario), and `write_scenario` feeds a run.

Notice what is *not* in the testbench: no clock or reset driving, no handshake logic, no per-cycle
stepping, no golden written into the graph. Those are the framework's job — you described *what* is
connected to *what*, and the simulation supplies the *how*.

One deliberate subtlety, called out in the source: the crossbar **models contention** between the two
bundles, whereas the XSI slave models do not. The two rungs describe slightly different systems on
purpose, which is part of why the pysim timing runs optimistic (see [below](#measuring-timing)).

## The scenario is stated once

`write_scenario` is the **single** writer for both backends. pysim's `run_copy` calls it with a temp
root before `run_sim`; the [XSI rung](./testbench.md)'s `write_mem_copy_xsi_bundles` is a one-line
wrapper that calls the *same* method with the `xsi/` dir. Both materialize
`vectors/{s_cmd,mem_in,golden}` from the testbench's own `cmd_words` / `mem_image` / `golden_image`, so
the pysim run and the RTL run cannot start from different bytes — one writer, two roots.

The driver reads that on-disk bundle in `pre_sim`, resolving the relative `in_bundle` against the root
`write_scenario` set — the exact point, and the exact bytes, the C++ `AxisMaster` reads. One
convention on both sides: *the bundle files exist before the sim starts.*

One asymmetry remains, worth naming: the **commands** genuinely round-trip through the on-disk bundle
in pysim (the driver loads `vectors/s_cmd` in `pre_sim`), while the **memory** is still seeded
in-process — `mem_in` is written for the XSI memory but pysim reads the seed directly. Both derive from
the one scenario; unifying the memory load too is a pending step (the arena is sized smaller than the
full `mem_in` image, so it needs a clip).

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
got = mem._mem.read(job.dst_off * bpw, job.n_words)
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

| measure             | value                             |
| ------------------- | --------------------------------- |
| one job, end to end | 159 cycles                        |
| 16 jobs, end to end | 2214 cycles                       |
| per-transfer span   | read 137 cycles, write 140 cycles |

`--through pysim` records the end-to-end number to `results/pysim.json` (`end_cycles: 2214`); the
per-transfer spans come off the run object as above.

Those decompose exactly — `159 + 15 x 137 = 2214` — into a **fill latency** of 159 cycles and a
**steady-state period** of 137 cycles per job. And the period is the interesting part: a job reads for
137 and writes for 140, so a sequential design would need ~277 cycles per job. Getting ~137 means the
read and the write are overlapping — `max(read, write)`, not `read + write`. The pipeline works.

### How close is it to the RTL?

The same scenario at the [XSI rung](./testbench.md) reports per-job completion cycles, so the two are
directly comparable:

|                         | pysim | RTL (XSI) | pysim / RTL |
| ----------------------- | ----- | --------- | ----------- |
| fill (first completion) | 159   | 171       | 93%         |
| steady-state period     | 137   | 178       | 77%         |
| total, 16 jobs          | 2214  | 2835      | 78%         |

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
