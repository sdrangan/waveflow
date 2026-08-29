---
title: Python simulation
parent: A memory reached three ways
nav_order: 2
has_children: false
---

# Python simulation

Python simulation is fast and needs no toolchain, so the design runs in SimPy long before Vitis is
invoked. This page runs it, shows the scenario it runs, and records the timing the
[trace page](timing.md) later compares against RTL.

```bash
cd examples/bram_access
python bram_access_build.py --through pysim
```

## The testbench is the DUT and six BFMs

`BramAccessTB` wires three `StreamDriver`s to the command and payload inputs and three sinks to the
answers. **The memory is not in the testbench** — it is inside the DUT's wrapper — and that absence
is the property that keeps the RTL harness small later: the elaborated design's only pins are
AXI-Stream, so the BFM library needs no memory model at all.

```python
from waveflow.simulation.simulation import Simulation
from waveflow.build.composite_gen import tb_top_spec
from examples.bram_access.bram_access import BramAccessTB

spec = tb_top_spec(BramAccessTB(name="tb", sim=Simulation()))
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
than of words — `WriteComputeCmd` and `ReadCmd` objects, whose fields the scenario sets by name:

```python
from examples.bram_access.bram_access import BramOp, scenario_zero

sc = scenario_zero()
print("writes:", [(int(c.tid), BramOp(int(c.opcode)).name, int(c.nsamp), int(c.waddr))
                  for c in sc.cmd_w])
print("reads :", [(int(c.tid), int(c.nsamp), int(c.raddr)) for c in sc.cmd_r])
print("payload words:", len(sc.data_w))
```

```
writes: [(1, 'WRITE', 256, 0), (2, 'WRITE', 4, 1020), (3, 'WRITE', 64, 64), (4, 'WRITE', 32, 512), (5, 'COMPUTE', 32, 512), (6, 'COMPUTE', 8, 1020)]
reads : [(1, 1, 0), (2, 1, 1), (3, 1, 7), (4, 1, 255), (5, 1, 128), (6, 8, 1020), (7, 64, 0), (8, 128, 128), (9, 4, 1020), (10, 32, 512)]
payload words: 356
```

Reading it in order, as `(tid, opcode, nsamp, addr)`:

- `WRITE(256, 0)` — the witness's ramp, `buf[i] = i + 100`. Completing it is also what arms the
  reader.
- `WRITE(4, 1020)` — a **sentinel**, `500…503`, at the top of the memory.
- `WRITE(64, 64)` — phase 2's payload, which runs while a read is outstanding.
- `WRITE(32, 512)` then `COMPUTE(32, 512)` — phase 3: seed a region with a known ramp, then rewrite
  it in place as `x*3 + 1`. The seed is not optional; computing over never-written memory is not a
  check, because pysim would read `0` from a zeroed array and the RTL `X`, and `0*3+1` is a
  plausible-looking `1`.
- `COMPUTE(8, 1020)` — **out of range** (`1020 + 8 > 1024`), so refused whole. It carries no payload,
  and the refusal must leave the sentinel alone.
- The five one-word reads are the witness's addresses.
- `read(8, 1020)` — **out of range**, refused. It returns *no* data words.
- `read(64, 0)` — the overlapping read.
- `read(128, 128)` — a stretch of the ramp nothing rewrites. It is there for **spacing**: see below.
- `read(4, 1020)` — reads the sentinel back, and this is what proves the refused command touched
  nothing.
- `read(32, 512)` — reads phase 3 back, and it is checked element by element against `x*3 + 1`.

**The order of both streams is load-bearing**, which is worth stating because it does not look like
it. Once the arming token is spent the two tasks are concurrent and ordered only by their own command
streams, so a `COMPUTE` writing a region the reader is reading is a genuine read-during-write
collision. The reader is much faster per command than the writer — a read answers `nsamp` words and
moves on, while a write consumes them and a compute spends `2 x nsamp` cycles — so anything the
writer must finish first is placed **early for the writer and late for the reader**, and the 128-word
read buys the spacing that makes the difference. Phase 2 is the opposite requirement and is placed
against it. None of that is trusted: the
[hazard scan](timing.md#the-hazard-that-cannot-be-heard) asserts scenario zero has no collisions,
against a positive control that has them, and it is what caught the first draft of this ordering.

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
from examples.bram_access.bram_access import scenario_zero, write_scenario
from waveflow.utils.burst_io import read_burst_bundle

with tempfile.TemporaryDirectory() as tmp:
    write_scenario(tmp, scenario_zero())
    for name in ("cmd_w", "cmd_r", "data_w"):
        bursts = read_burst_bundle(Path(tmp) / "vectors" / name)
        print(f"{name:7s} {len(bursts):3d} bursts, {sorted({len(b) for b in bursts})} word(s) each")
```

```
cmd_w     6 bursts, [4] word(s) each
cmd_r    10 bursts, [3] word(s) each
data_w    4 bursts, [4, 32, 64, 256] word(s) each
```

**A command is one burst.** `get(WriteComputeCmd)` asks for the schema's whole word count in a single
call, and a pysim slave dequeues a whole burst per call — so a command split across bursts would be
read a fragment at a time and the design would be back to counting words. Four and three are the
schemas' numbers, not ones written down here: `write_scenario` calls `serialize`, and the length is
whatever that returns. The write/compute command is the longer of the two because it carries an
opcode, and an opcode is a field, and a field here is a word.

**A payload is one burst too** — its command's `nsamp` words — **and only a `WRITE` has one.** There
are six commands on `cmd_w` and only four payload bursts, because the two `COMPUTE`s read the words
they rewrite and take nothing off `data_w` at all. `write_scenario` derives that split from the
opcodes rather than from how a scenario happened to build its `data_w`, and checks the total: a
payload framed against *every* command would hand each later `WRITE` the previous one's data,
silently, from the first `COMPUTE` onwards, with every response still saying `OK`.

`get_pipelined(count=nsamp)` reads a payload in one call, because a pysim slave dequeues a whole
burst per call and truncation *discards* the remainder.

This replaced a one-word-per-burst framing whose stated reason was "one pysim firing equals one RTL
firing" — the rationale for the per-element loops the design used to have. It is retired, along with
the loops: a pysim body that reads a word at a time is not a faithful twin of an `II=1` C++ loop, it
is a design that has opted out of the LT model. See [the three access cases](../../guide/interface/overview.md#the-three-access-cases).

**The framing is not purely a pysim concern, and it was measured rather than assumed.** `words.bin`
is byte-identical — the same words in the same order — but `bounds.bin` is not, and both backends
read it, so the XSI `AxisMaster` asserts `TLAST` once per command instead of once per word. The DUT
does not care: `bram_write_compute_task` reads a raw `hls::stream` `nsamp` times and never inspects
`TLAST` on the payload. When that framing changed, the RTL cycle count did not move at all — and
neither did any figure rendered from the traced waveform, which is the stronger evidence that it was
the same run.

## Running it, and what comes back

A captured response is **deserialized**, exactly the way the design reads one — not sliced by hand,
which would put the field layout back in a second place:

```python
import numpy as np
from examples.bram_access.bram_access import (
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
resp_w: [(1, 'OK'), (2, 'OK'), (3, 'OK'), (4, 'OK'), (5, 'OK'), (6, 'OUT_OF_RANGE')]
resp_r: [(1, 'OK'), (2, 'OK'), (3, 'OK'), (4, 'OK'), (5, 'OK'), (6, 'OUT_OF_RANGE'), (7, 'OK'), (8, 'OK'), (9, 'OK'), (10, 'OK')]
data_r: 233 words; [100, 101, 107, 355, 228] ... [985, 988, 991, 994]
```

**`OUT_OF_RANGE`, not `1`.** The status is an `EnumField` over an `IntEnum`, so the schema, the
generated C++ `enum class`, the model and this listing all spell it the same way. A capture full of
`0`s and `1`s is a capture nothing can name.

Everything the design claims is in those three lines:

- The **witness's five values** — `100, 101, 107, 355, 228` — come back for addresses
  `0, 1, 7, 255, 128`.
- The sixth *write/compute* command is refused and says so — and the refusal comes back on `tid=6`,
  the command that caused it. The sentinel at `1020…1023` is read back intact afterwards, which is
  what proves the refusal applied **nothing** rather than the four words that would have fitted.
- The sixth *read* is refused and says so, and contributes **zero** of the 233 data words. That is
  the whole argument for the read response existing: a consumer waiting on the data stream would have
  seen only silence.
- The tail `985 988 991 994` is the `COMPUTE` region read back. It steps by 3 because the seed was a
  ramp and the operation is `x*3 + 1` — which is the point of multiplying rather than incrementing:
  over a ramp, `x + 1` would look correct even if the address were off by one.

The check is one function both backends call, so neither can drift from the other:

```python
from examples.bram_access.bram_access import captured, check_outputs, run_pysim, scenario_zero

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
from examples.bram_access.bram_access import WORD_BW, WriteComputeCmd, WriteResp

print("at", WORD_BW, "bits: command", WriteComputeCmd.nwords_per_inst(WORD_BW),
      "words, response", WriteResp.nwords_per_inst(WORD_BW), "words")
try:
    WriteResp.nwords_per_inst(16)
except ValueError as e:
    print("at 16 bits:", str(e).split(".")[0])
```

```
at 64 bits: command 4 words, response 2 words
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
from examples.bram_access.bram_access import run_pysim, scenario_zero

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
first data word at cycle 263 - last at 529
the 64-word read arrives with word-to-word gaps [1]
write responses at [258, 259, 262, 263, 326, 327, 358, 359, 424, 425, 426, 427]
```

Two things are already visible:

- **One word per cycle** through the 64-word read. That is the throughput claim, and it will hold at
  RTL too.
- **Twelve timestamps for six responses.** A sink stamps every *word*, and a response is two of them.
  So anything that indexes an arrival-cycle array by *response* has to convert — through the schema,
  not through a literal:

```python
import numpy as np
from examples.bram_access.bram_access import (
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
read window [290, 353], phase-2 write done at 327
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
  and what [this example's 64-bit geometry](../../guide/interface/bram.md#the-addressing-convention)
  exists to expose.
- **Whether `mode=bram` took effect.** An unsized pointer degrades to an `ap_vld` scalar port
  silently, and no Python run can see that. The port list can, and
  [code generation](codegen.md) checks it.
- **What the in-place `COMPUTE` costs.** pysim charges `ii_for(2)` because the port *declares* one
  access per cycle; whether Vitis then schedules the loop at 2 is a synthesis question, and it is
  [measured on the waveform](timing.md#what-it-costs-to-read-a-word-you-are-about-to-write).

## See also

- [Python model](python.md) — the code this page runs.
- [Code generation](codegen.md) — what the same graph lowers to.
- [Reading the trace](timing.md) — the same measurements, from the RTL side.
