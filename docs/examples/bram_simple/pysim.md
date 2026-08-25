---
title: Python simulation
parent: Shared memory between two modules
nav_order: 3
has_children: false
---

# Python simulation

Python simulation is fast and needs no toolchain, so the design runs in SimPy long before Vitis is
invoked. This page runs it, shows the scenario it runs, and records the timing the
[trace page](timing.md) later compares against RTL.

```bash
cd examples/bram_simple
python bram_simple_build.py --through pysim
```

## The testbench is the DUT and six BFMs

`BramSimpleTB` wires three `StreamDriver`s to the command and payload inputs and three sinks to the
answers. **The memory is not in the testbench** — it is inside the DUT's wrapper — and that absence
is the property that keeps the RTL harness small later: the elaborated design's only pins are
AXI-Stream, so the BFM library needs no memory model at all.

```python
from waveflow.simulation.simulation import Simulation
from waveflow.build.composite_gen import tb_top_spec
from examples.bram_simple.bram_simple import BramSimpleTB

spec = tb_top_spec(BramSimpleTB(name="tb", sim=Simulation()))
print([m.cls for m in spec.models])
```

```
['AxisMaster', 'AxisMaster', 'AxisSlave', 'AxisMaster', 'AxisSlave', 'AxisSlave']
```

Six models, six pins, and not one of them is a memory. **There is no BRAM BFM anywhere in this
repo**, and that is a stronger story than having one: in RTL simulation the memory is `bram_t2p.v`
*itself*, compiled into the simulation beside the synthesized kernel, so there is no second
implementation that could disagree with the first.

## Scenario zero

The scenario is one on-disk source that both backends play. Commands are `(pointer, count)` pairs,
flattened onto the command stream:

```python
from examples.bram_simple.bram_simple import scenario_zero

sc = scenario_zero()
print("writes:", [tuple(sc.cmd_w[i:i + 2]) for i in range(0, len(sc.cmd_w), 2)])
print("reads :", [tuple(sc.cmd_r[i:i + 2]) for i in range(0, len(sc.cmd_r), 2)])
print("payload words:", len(sc.data_w))
```

```
writes: [(0, 256), (1020, 4), (1020, 8), (64, 64)]
reads : [(0, 1), (1, 1), (7, 1), (255, 1), (128, 1), (1020, 8), (0, 64), (1020, 4)]
payload words: 332
```

Reading it in order:

- `write(0, 256)` — the witness's ramp, `buf[i] = i + 100`.
- `write(1020, 4)` — a **sentinel**, `500…503`, at the top of the memory.
- `write(1020, 8)` — **out of range** (`1020 + 8 > 1024`), so refused whole. Its 8 payload words are
  consumed and dropped.
- `write(64, 64)` — phase 2's payload, which runs while a read is outstanding.
- The five one-word reads are the witness's addresses.
- `read(1020, 8)` — **out of range**, refused. It returns *no* data words.
- `read(0, 64)` — the overlapping read.
- `read(1020, 4)` — reads the sentinel back, and this is what proves the refused write touched
  nothing.

**The sentinel is not decoration.** Checking that a refused write left words alone cannot be done
against *never-written* memory: pysim returns 0 from a zeroed numpy array and the RTL returns `X`,
because `bram_t2p.v`'s `reg [DW-1:0] mem [...]` has no initial value. The first RTL run of this check
returned `0xFFFF_FFFF_FFFF_FFFF` where pysim said `0`. A legal write puts a known value there first.

**One word per burst**, and that is not a detail. Each task consumes one word per `get`, and a pysim
slave dequeues a whole burst per call with truncation *discarding* the remainder — so a multi-word
burst would be one pysim firing against several RTL firings, and the two backends would be running
different designs. The XSI `AxisMaster` reads the flat `words.bin` and never sees the burst bounds,
so the stimulus is byte-identical either way.

```python
import tempfile
from pathlib import Path
from examples.bram_simple.bram_simple import scenario_zero, write_scenario
from waveflow.utils.burst_io import read_burst_bundle

with tempfile.TemporaryDirectory() as tmp:
    write_scenario(tmp, scenario_zero())
    bursts = read_burst_bundle(Path(tmp) / "vectors" / "cmd_w")
    print(len(bursts), "bursts,", sorted({len(b) for b in bursts}), "word(s) each")
```

```
8 bursts, [1] word(s) each
```

## Running it, and what comes back

```python
from examples.bram_simple.bram_simple import captured, run_pysim, scenario_zero

sc = scenario_zero()
tb = run_pysim(sc=sc)
resp_w, data_r, resp_r = captured(tb)
print("resp_w:", [int(v) for v in resp_w])
print("resp_r:", [int(v) for v in resp_r])
print("data_r:", data_r.size, "words;", [int(v) for v in data_r[:5]],
      "...", [int(v) for v in data_r[-4:]])
```

```
resp_w: [0, 0, 1, 0]
resp_r: [0, 0, 0, 0, 0, 1, 0, 0]
data_r: 73 words; [100, 101, 107, 355, 228] ... [500, 501, 502, 503]
```

Everything the design claims is in those three lines. `0` is `ST_OK` and `1` is `ST_OUT_OF_RANGE`:

- The **witness's five values** — `100, 101, 107, 355, 228` — come back for addresses
  `0, 1, 7, 255, 128`.
- The third *write* is refused and says so, and the tail `500 501 502 503` is the sentinel: the
  refusal applied **nothing**, not the four words that would have fitted.
- The sixth *read* is refused and says so, and contributes **zero** of the 73 data words. That is the
  whole argument for the read response existing: a consumer waiting on the data stream would have seen only
  silence.

The check is one function both backends call, so neither can drift from the other:

```python
from examples.bram_simple.bram_simple import captured, check_outputs, run_pysim, scenario_zero

sc = scenario_zero()
check_outputs(*captured(run_pysim(sc=sc)), sc=sc, where="pysim: ")
print("scenario zero matches")
```

```
scenario zero matches
```

## The design is width-parametric, and the witness survives it

The gated configuration is 64 bits, because that is where the byte/word address convention is
actually exercised. But the *witness's own* geometry was 16, and its values fit in 16 bits — so the
same scenario, at the same design, gives the same five answers:

```python
from examples.bram_simple.bram_simple import captured, check_outputs, run_pysim, scenario_zero

sc = scenario_zero()
for width in (16, 64):
    _rw, data_r, _rr = captured(run_pysim(sc=sc, bitwidth=width))
    check_outputs(*captured(run_pysim(sc=sc, bitwidth=width)), sc=sc, where=f"W={width}: ")
    print(width, [int(v) for v in data_r[:5]])
```

```
16 [100, 101, 107, 355, 228]
64 [100, 101, 107, 355, 228]
```

## Recording the timing

`StreamSink` keeps words; it does not keep *when*. The example subclasses it as `TimedStreamSink`,
which also records the arrival cycle of every word — because objective 4 is a claim about *when* a
word appears, and the two backends have to be comparable in the same units. It is a subclass rather
than a framework change because the timestamp is a measurement of **this example**, not a property
every stream sink should carry.

```python
import numpy as np
from examples.bram_simple.bram_simple import run_pysim, scenario_zero

sc = scenario_zero()
tb = run_pysim(sc=sc)
cycles = np.asarray(tb.data_r_snk.cycles)
lo, hi = sc.cadence_read
print("first data word at cycle", int(cycles[0]), "- last at", int(cycles[-1]))
print("the 64-word read arrives with word-to-word gaps",
      sorted(set(np.diff(cycles[lo:hi]).tolist())))
print("write responses at", [int(c) for c in tb.resp_w_snk.cycles])
```

```
first data word at cycle 260 - last at 345
the 64-word read arrives with word-to-word gaps [1]
write responses at [257, 261, 269, 333]
```

Two things are already visible:

- **One word per cycle** through the 64-word read. That is the throughput claim, and it will hold at
  RTL too.
- **The overlap is real.** The fourth write command finishes at cycle 333, and the 64-word read runs
  from 276 to 339 — so the write is live *inside* the read. That is checked rather than eyeballed:

```python
import numpy as np
from examples.bram_simple.bram_simple import run_pysim, scenario_zero

sc = scenario_zero()
tb = run_pysim(sc=sc)
cycles = np.asarray(tb.data_r_snk.cycles)
lo, hi = sc.overlap_read
when = int(tb.resp_w_snk.cycles[sc.overlap_write_resp])
print(f"read window [{int(cycles[lo])}, {int(cycles[hi - 1])}], phase-2 write done at {when}")
print("overlapped:", int(cycles[lo]) <= when <= int(cycles[hi - 1]))
```

```
read window [276, 339], phase-2 write done at 333
overlapped: True
```

Their address ranges are disjoint — `0…63` read, `64…127` written — so **the words would be
identical whether or not they overlapped**. That is exactly why the overlap is asserted in cycles
rather than in data, here and at RTL.

## What pysim cannot tell you

Three things, and it is worth being explicit about them:

- **Whether the ranges were disjoint in every cycle.** pysim's memory access is untimed, so there is
  no cycle in which a collision could be observed. The hazard is an RTL question and is answered on
  the [trace page](timing.md#the-hazard-that-cannot-be-heard).
- **Whether the addressing convention is right.** The byte/word scaling lives in the *wrapper*, which
  pysim does not have. A mis-addressed design passes here and fails at RTL — which is what happened,
  and what this example's 64-bit geometry exists to expose.
- **Whether `mode=bram` took effect.** An unsized pointer degrades to an `ap_vld` scalar port
  silently, and no Python run can see that. The port list can, and
  [code generation](codegen.md) checks it.

## See also

- [Python model](python.md) — the code this page runs.
- [Code generation](codegen.md) — what the same graph lowers to.
- [Reading the trace](timing.md) — the same measurements, from the RTL side.
