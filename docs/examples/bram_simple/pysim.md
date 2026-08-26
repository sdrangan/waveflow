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

The scenario is one on-disk source that both backends play, and it is a list of **messages** rather
than of words — `WriteCmd` and `ReadCmd` objects, whose fields the scenario sets by name:

```python
from examples.bram_simple.bram_simple import scenario_zero

sc = scenario_zero()
print("writes:", [(int(c.tid), int(c.nsamp), int(c.waddr)) for c in sc.cmd_w])
print("reads :", [(int(c.tid), int(c.nsamp), int(c.raddr)) for c in sc.cmd_r])
print("payload words:", len(sc.data_w))
```

```
writes: [(1, 256, 0), (2, 4, 1020), (3, 8, 1020), (4, 64, 64)]
reads : [(1, 1, 0), (2, 1, 1), (3, 1, 7), (4, 1, 255), (5, 1, 128), (6, 8, 1020), (7, 64, 0), (8, 4, 1020)]
payload words: 332
```

Reading it in order, as `(tid, nsamp, addr)`:

- `write(nsamp=256, waddr=0)` — the witness's ramp, `buf[i] = i + 100`.
- `write(4, 1020)` — a **sentinel**, `500…503`, at the top of the memory.
- `write(8, 1020)` — **out of range** (`1020 + 8 > 1024`), so refused whole. Its 8 payload words are
  consumed and dropped.
- `write(64, 64)` — phase 2's payload, which runs while a read is outstanding.
- The five one-word reads are the witness's addresses.
- `read(8, 1020)` — **out of range**, refused. It returns *no* data words.
- `read(64, 0)` — the overlapping read.
- `read(4, 1020)` — reads the sentinel back, and this is what proves the refused write touched
  nothing.

`tid` here simply counts the commands on each stream. Any scheme would do — what matters is that the
response **echoes** it, so a reply can be matched to the command that caused it without relying on
the order they came back in.

**The sentinel is not decoration.** Checking that a refused write left words alone cannot be done
against *never-written* memory: pysim returns 0 from a zeroed numpy array and the RTL returns `X`,
because `bram_t2p.v`'s `reg [DW-1:0] mem [...]` has no initial value. The first RTL run of this check
returned `0xFFFF_FFFF_FFFF_FFFF` where pysim said `0`. A legal write puts a known value there first.

### Two framings, and both are load-bearing

The command bundles and the payload bundle are framed **differently**, and neither choice is
cosmetic:

```python
import tempfile
from pathlib import Path
from examples.bram_simple.bram_simple import scenario_zero, write_scenario
from waveflow.utils.burst_io import read_burst_bundle

with tempfile.TemporaryDirectory() as tmp:
    write_scenario(tmp, scenario_zero())
    for name in ("cmd_w", "cmd_r", "data_w"):
        bursts = read_burst_bundle(Path(tmp) / "vectors" / name)
        print(f"{name:7s} {len(bursts):3d} bursts, {sorted({len(b) for b in bursts})} word(s) each")
```

```
cmd_w     4 bursts, [3] word(s) each
cmd_r     8 bursts, [3] word(s) each
data_w  332 bursts, [1] word(s) each
```

**A command is one burst.** `get(WriteCmd)` asks for the schema's whole word count in a single call,
and a pysim slave dequeues a whole burst per call — so a command split across bursts would be read a
fragment at a time and the design would be back to counting words. Three is the schema's number, not
one written down here: `write_scenario` calls `serialize`, and the length is whatever that returns.

**The payload is one word per burst.** It is a data stream rather than a structured message, and
per-word framing is what keeps one pysim firing equal to one RTL firing: truncation *discards* the
remainder of a burst, so a multi-word payload burst would be one pysim firing against several RTL
firings and the two backends would be running different designs.

The XSI `AxisMaster` reads the flat `words.bin` and never sees the burst bounds, so the stimulus it
plays is byte-identical either way — the framing is a **pysim** concern only.

## Running it, and what comes back

A captured response is **deserialized**, exactly the way the design reads one — not sliced by hand,
which would put the field layout back in a second place:

```python
import numpy as np
from examples.bram_simple.bram_simple import (
    WORD_BW, BramStatus, ReadResp, WriteResp, captured, run_pysim, scenario_zero,
)

def responses(words, schema):
    per = schema.nwords_per_inst(WORD_BW)
    raw = np.asarray(words, dtype=np.uint64).ravel()
    return [(int(o.tid), BramStatus(int(o.status)).name)
            for o in (schema().deserialize(raw[i:i + per], word_bw=WORD_BW)
                      for i in range(0, raw.size, per))]

sc = scenario_zero()
resp_w, data_r, resp_r = captured(run_pysim(sc=sc))
print("resp_w:", responses(resp_w, WriteResp))
print("resp_r:", responses(resp_r, ReadResp))
print("data_r:", data_r.size, "words;", [int(v) for v in data_r[:5]],
      "...", [int(v) for v in data_r[-4:]])
```

```
resp_w: [(1, 'OK'), (2, 'OK'), (3, 'OUT_OF_RANGE'), (4, 'OK')]
resp_r: [(1, 'OK'), (2, 'OK'), (3, 'OK'), (4, 'OK'), (5, 'OK'), (6, 'OUT_OF_RANGE'), (7, 'OK'), (8, 'OK')]
data_r: 73 words; [100, 101, 107, 355, 228] ... [500, 501, 502, 503]
```

**`OUT_OF_RANGE`, not `1`.** The status is an `EnumField` over an `IntEnum`, so the schema, the
generated C++ `enum class`, the model and this listing all spell it the same way. A capture full of
`0`s and `1`s is a capture nothing can name.

Everything the design claims is in those three lines:

- The **witness's five values** — `100, 101, 107, 355, 228` — come back for addresses
  `0, 1, 7, 255, 128`.
- The third *write* is refused and says so — and the refusal comes back on `tid=3`, the command that
  caused it. The tail `500 501 502 503` is the sentinel: the refusal applied **nothing**, not the
  four words that would have fitted.
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

## The messages pin the stream width

This design used to run at 16 bits as well as 64, and was checked at both. **It cannot any more**,
and the reason is worth stating rather than discovering: the messages carry one field per word at the
design's width, and an `EnumField` may not straddle a word — so a 64-bit `status` is unreadable on a
narrower stream. The schema **raises** rather than mis-framing it, which is the right failure, but it
is a failure:

```python
from examples.bram_simple.bram_simple import WORD_BW, ReadCmd, WriteCmd, WriteResp

print("at", WORD_BW, "bits: command", WriteCmd.nwords_per_inst(WORD_BW),
      "words, response", WriteResp.nwords_per_inst(WORD_BW), "words")
try:
    WriteResp.nwords_per_inst(16)
except ValueError as e:
    print("at 16 bits:", str(e).split(".")[0])
```

```
at 64 bits: command 3 words, response 2 words
at 16 bits: Field 'BramStatusEnumField' with bitwidth 64 cannot fit into word_bw=16
```

That is the trade the schema bought: one author for every field layout, at the cost of a design that
is no longer width-agnostic. The witness's *values* are untouched — they are what the memory holds,
not what the command stream looks like.

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
first data word at cycle 261 - last at 353
the 64-word read arrives with word-to-word gaps [1]
write responses at [258, 258, 262, 262, 270, 270, 334, 334]
```

Two things are already visible:

- **One word per cycle** through the 64-word read. That is the throughput claim, and it will hold at
  RTL too.
- **Eight timestamps for four responses.** A sink stamps every *word*, and a response is two of them.
  So anything that indexes an arrival-cycle array by *response* has to convert — through the schema,
  not through a literal:

```python
import numpy as np
from examples.bram_simple.bram_simple import (
    WriteResp, resp_words, run_pysim, scenario_zero,
)

sc = scenario_zero()
tb = run_pysim(sc=sc)
cycles = np.asarray(tb.data_r_snk.cycles)
lo, hi = sc.overlap_read
last_word = resp_words(WriteResp, sc.overlap_write_resp + 1) - 1
when = int(tb.resp_w_snk.cycles[last_word])
print(f"read window [{int(cycles[lo])}, {int(cycles[hi - 1])}], phase-2 write done at {when}")
print("overlapped:", int(cycles[lo]) <= when <= int(cycles[hi - 1]))
```

```
read window [283, 346], phase-2 write done at 334
overlapped: True
```

`resp_words` is the conversion, and it goes through `nwords_per_inst` rather than through a `2`. The
response grew from one word to two the day it gained a `tid`; a literal would not have noticed, and
the index would have quietly pointed into the middle of a message.

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
