---
title: Python model
parent: Shared memory between two modules
nav_order: 2
has_children: false
---

# Python model

The code for the BRAM kernel is in [`examples/bram_simple\`](../../examples/bram_simple).  We begin with the python model defined in [`examples/bram_simple/bram_simple.py`](../../examples/bram_simple/bram_simple.py).

## Read and Write Transactions

The BRAM simple kernel exposes read and write transactions for an internal BRAM memory.
Both transactions use a standard command response protocol:

- **Write transaction**:
    - The calling module [is "calling module" the correct term?] sends a `WriteCmd` message with ... [describe the message] on streawm ..
    - The calling module  then sends data ...
    - The kernel responds with 

- **Read transaction**:
   [Give a similar description]

## Messages

The command response protocol thus requires four messages:

| message | fields |
|---|---|
| `WriteCmd` | `tid`, `nsamp`, `waddr` |
| `WriteResp` | `tid`, `status` |
| `ReadCmd` | `tid`, `nsamp`, `raddr` |
| `ReadResp` | `tid`, `status` |

As usual, the messages are defined as [`DataList` data schemas](../../guide/schema/python/datalists.md).   For example, the `WriteCmd` and `WriteResp` are given by:

```python
class WriteCmd(DataList):
    include_filename: ClassVar[str | None] = "bram_write_cmd.h"
    elements: ClassVar[dict] = {
        "tid":   {"schema": Word64, "description": "transaction id, echoed on the response"},
        "nsamp": {"schema": Word64, "description": "payload words this command carries"},
        "waddr": {"schema": Word64, "description": "first word address written"},
    }

class WriteResp(DataList):
    [Give the Write resp]
```

The `include_filename` indicates the location of the generated include file that will be discussed in the [codegen section](./codegen.md). The `WriteResp` includes a `status` field given by an `EnumField` indicating if the write address is valid:

```python
class BramStatus(IntEnum):
    OK = 0
    OUT_OF_RANGE = 1

BramStatusField = EnumField.specialize(enum_type=BramStatus, bitwidth=WORD_BW)
```

The `tid` is what makes a response usable from a second thread. A host correlates a reply to the
command it issued instead of inferring it from ordering — the same reason the RF transmit stream's
response carries one.

The read command and response are similar.

## Modules

As shown in the diagram in the [introduction](./index.md), the BRAM simple kernel is composed of three `HwModule` classes:  The BRAM itself, a `BramWriteCmd` that processes the write transactions; and a `BramReadCmd` the processes the read transactions.  

The `BramWriteCmd` uses standard stream interfaces for the command, response and data along with a [`BramIFMaster`](../../guide/interface/bram.md) interface for the BRAM:

```python
class BramWriteCmd(FreeRunMod):
    def __post__init(self):
        self.cmd_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.data_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_data", bitwidth=w)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w, depth=d,
                                access="write")
        self.resp_w = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w)
        self.go_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
```

The body for 

```python
cmd = yield from self.cmd_w.get(WriteCmd)
wp, n = int(cmd.waddr), int(cmd.nsamp)
ok = n <= int(self.depth) and wp <= int(self.depth) - n
for i in range(n):
    x = yield from _word(self.data_w)
    if ok:                                   # refused: consumed, then dropped on the floor
        self.buf_w.mem_write(wp + i, x)
resp = WriteResp()
resp.tid = cmd.tid
resp.status = BramStatus.OK if ok else BramStatus.OUT_OF_RANGE
yield from self.resp_w.write(resp)
```

**The command is read in one call, and the response written in one.** That is the whole point of
declaring the schema, and it is the [fourth row of the four ways to move
data](../../guide/interface/stream.md#the-four-ways-to-move-data): `get(WriteCmd)` derives the word
count from `WriteCmd.nwords_per_inst(bitwidth)` and deserializes; `write(resp)` serializes. Neither
end counts words.

> **Declaring the `DataList` is not enough.** The failure this design was changed to remove is
> declaring it and then unpacking it by hand anyway — `wp = yield from _word(...)` twice. That
> re-authors the field layout in the one place nothing checks it against the **generated C++ header
> the kernel compiles against**, which is the same defect as hand-rolled element packing, one level
> up. Check the read side, in both backends.

The **payload** is different and stays word-at-a-time. It is a data stream, not a structured message:
there is no layout to agree about, and one word per burst is what keeps one pysim firing equal to one
RTL firing.

Two more details that are not style:

- **`ok` is spelled `n <= N and wp <= N - n`, never `wp + n <= N`.** In the C++ twin the operands are
  unsigned, and the sum of two legal-looking values wraps — turning an out-of-range command into an
  accepted one at exactly the widths where a memory is most likely to be full. Neither term of the
  spelling used can overflow.
- **A refused command still consumes its payload.** The payload belongs to the command; leaving it in
  the stream would shift every later command's data by `nsamp` words and turn one caller error into a
  corrupted run.

`mem_write` is a **plain method call**, not a generator. That is the interface saying no simulated
time passes — see [the read path](#the-read-path-is-the-one-that-costs-something) below.

## The read task

The mirror image, plus the arming:

```python
if not self.armed:
    yield from _word(self.go_in)
    self.armed = True
cmd = yield from self.cmd_r.get(ReadCmd)
rp, n = int(cmd.raddr), int(cmd.nsamp)
ok = n <= int(self.depth) and rp <= int(self.depth) - n
```

One token, consumed once, and then the reader is command-driven forever. Hoisting it *out* of the
per-word loop is not a micro-optimisation: a conditional blocking read inside a pipelined body is a
data-dependent stall, which is the shape Vitis reports as `[HLS 200-878] Unable to schedule the loop
exit test` and which pins other designs' bodies at II=2. The question here ("has anything been
written yet?") is about the whole run rather than about this word, so it can be asked once.

## The read path is the one that costs something

This is the part worth reading slowly.

A `BramIF` access is **untimed in pysim, on purpose**. A BRAM answer is deterministic, unarbitrated
and one cycle, so a discrete-event model of it would add a SimPy timestep and no fidelity —
`mem_read` and `mem_write` are plain methods rather than generators, and **the absence of the
`yield` is the interface stating that no time passes**. (Contrast [AXI-MM](../../guide/interface/aximm.md),
where the bus, the arbitration and the burst *are* the point of having a model.)

What that leaves out is not throughput. At II=1 a pipelined reader still answers one word per cycle
whatever the memory's latency is — the pipeline hides it. What it leaves out is **when the first
answer appears**, which at RTL is `READ_LATENCY` cycles after the address. So the model pays it
once per command, outside the per-word loop:

```python
if ok and n:
    if self.model_read_latency:
        yield self.timeout(int(self.buf_r.read_latency) / float(self.clk.freq))
    for i in range(n):
        val = self.buf_r.mem_read(rp + i)
        yield from self.data_r.write(np.array([val], dtype=np.uint64))
```

**The number is never written down in Python.** `BramIFMaster.read_latency` resolves through the
bound `BramIF` to the memory module, which reads it out of the Verilog's `localparam READ_LATENCY`.
A student writing `yield self.timeout(1)` with a hard-coded `1` is doing exactly the thing the
framework refuses to do — and the framework refuses it by **raising when the port is unbound**:

```python
from waveflow.build.elaborate import ElabContext
from waveflow.hw.bram import BramIFMaster

loose = BramIFMaster(sim=ElabContext(), name="loose", bitwidth=64, depth=1024, access="read")
try:
    loose.read_latency
except ValueError as e:
    print(str(e).splitlines()[0][:61])
```

```
BramIFMaster 'loose' is not bound to a BramIF, so there is no
```

Bind it to a memory and the same property answers, from the artifact:

```python
from waveflow.build.elaborate import elaborate
from examples.bram_simple.bram_simple import BramSimple

comp = elaborate(BramSimple, {"bitwidth": 64, "depth": 1024}, name="bram_simple")
print(comp.rd.buf_r.read_latency, comp.mem.read_latency)
```

```
1 1
```

`model_read_latency` is a flag on the module rather than a constant, and it exists so the difference
can be **measured** rather than asserted from a docstring. [Reading the trace](timing.md) subtracts
the two runs.

## The kernel reads the same messages the same way

The hand-written task bodies are the C++ half of the four rows above, and they are worth reading
beside the Python:

```cpp
    WriteCmd c;
    c.read_stream<W>(cmd);
    bool ok = bram_cmd_in_range<W, N>(c.waddr, c.nsamp);
```

```cpp
    WriteResp r;
    r.tid = c.tid;
    r.status = ok ? BramStatus::OK : BramStatus::OUT_OF_RANGE;
    r.write_stream<W>(resp);
```

`read_stream` and `write_stream` come from `bram_write_cmd.h` and `bram_write_resp.h` — the headers
the schemas generate — so the kernel and the model cannot disagree about the layout. The
[kernel transfer reference](../../guide/custom_hooks/reference.md#mapping-the-python-transfer-interfaces-to-the-kernel)
maps every Python call to its HLS twin:

| Python | HLS |
|---|---|
| `get(Schema)` | `Schema c; c.read_stream<W>(s);` — **one call, never `n` × `s.read()`** |
| `write(obj)` | `obj.write_stream<W>(s)` |
| `get(nwords_max=1)` | `s.read()` — the payload, and only the payload |

What is **not** generated is the range check. That is a piece of the design's *logic* rather than of
its message layout, so it stays hand-written in `src/bram_cmd_range.h` — layout is the thing that must
have exactly one author.

## The composite

The registrations *are* the design:

```python
self.add_comp(self.wr)
self.add_comp(self.rd)

go_if = StreamIF(name=f"{self.name}_go_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
go_if.bind(ep_name="master", endpoint=self.wr.go_out)
go_if.bind(ep_name="slave", endpoint=self.rd.go_in)
self.add_if(go_if)

self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem", dwidth=w, depth=d)
self.add_rtl_mod(self.mem)
w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
self.add_rtl_if(w_if)
```

`add_rtl_mod` and `add_rtl_if` are different registries from `add_comp` and `add_if`, and that
difference is the mechanism: the walks that derive tasks and channels read the *other* two, so a
memory is never asked for a `kernel_task()` it does not have, and the accessor's port never vanishes
into a FIFO. The result is visible on the elaborated graph:

```python
from waveflow.build.elaborate import elaborate
from examples.bram_simple.bram_simple import BramSimple

comp = elaborate(BramSimple, {"bitwidth": 64, "depth": 1024}, name="bram_simple")
print([n for n, _ep in comp.boundary])
print([type(m).__name__ for m in comp.rtl_mods.values()],
      [c.name for c in comp.internal_edges])
```

```
['cmd_w', 'data_w', 'buf_w', 'resp_w', 'cmd_r', 'buf_r', 'data_r', 'resp_r']
['T2pBram'] ['go']
```

`buf_w` and `buf_r` are on the boundary — ports of the kernel, joined to the memory one level up.
`go` is not: it is an internal channel and both its endpoints left the boundary.

**`mem`, not `buf`.** The attribute name becomes the Verilog *instance* name, and `buf` is a Verilog
primitive gate — `bram_t2p #(...) buf (...)` is a syntax error. The wrapper emitter refuses reserved
names by name, rather than letting `xvlog` fail on something that mentions no Python.

## The memory module

`T2pBram` is framework, in [`waveflow/hw/bram.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/hw/bram.py).
Two of its properties are worth knowing:

```python
from waveflow.hw.bram import T2pBram, ramb18_count
from waveflow.build.elaborate import ElabContext

mem = T2pBram(sim=ElabContext(), name="mem", dwidth=64, depth=1024)
print(mem.addr_bits, mem.read_latency, ramb18_count(1024, 64))
```

```
10 1 4
```

- **`addr_bits`** refuses a depth that is not a power of two, rather than rounding up. The Verilog
  indexes `mem[addr[AW-1:0]]`, so any other depth aliases high addresses onto low ones silently, and
  rounding up would buy storage the caller did not ask for without making the wrap go away.
- **`read_latency`** is a property that reads the artifact, not a field anybody can set.
- **`ramb18_count`** is the footprint by *geometry* — 4 block RAMs for 1024 × 64 — and it is declared
  rather than measured because `csynth` of the kernel reports **no BRAM at all**: the memory is
  outside it. See [code generation](codegen.md#what-csynth-does-not-count).

The pysim storage is a zeroed numpy array, and `store` / `load` **refuse an out-of-range address**
rather than wrapping it — because the RTL wraps silently and a silent wrap is the bug worth catching
early.

## See also

- [Python simulation](pysim.md) — running this model, and the scenario it runs.
- [BRAM — memory between modules](../../guide/interface/bram.md) — the interface reference.
- [A module realized as Verilog](../../guide/comp_codegen/rtl_module.md) — `rtl_module()`, and the
  latency single-source rule this page relies on.


===

Text to delete:

[This is gibberish.  Some internal rambling about some weird error, I guess?  If useful, add it to detail notes in codeegn]

`BramIFMaster` is the **accessor's** end — a task's window onto storage it does not own. Two of its
arguments are load-bearing:

- **`depth`** becomes the C++ array's *size*. `mode=bram` on an unsized pointer degrades to an
  `ap_vld` scalar port **silently**: no warning, a clean `csynth`, and a design elaborated against a
  memory that is not there. The size is what makes the pragma take effect.
- **`access`** is declared, never inferred, and is checked when the interface binds. A port used both
  ways is what Vitis refuses inside a kernel, and it is no safer outside one.