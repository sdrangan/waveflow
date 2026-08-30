---
title: Python model
parent: A memory reached three ways
nav_order: 1
has_children: false
---

# Python model

The code for the BRAM kernel is in [`examples/bram_access`](../../../examples/bram_access).  We begin
with the python model defined in
[`examples/bram_access/bram_access.py`](../../../examples/bram_access/bram_access.py).

## The three transactions

The kernel exposes **three** transactions over one memory, and each is a command with a response:

| transaction | host sends | kernel answers | what it does |
|---|---|---|---|
| `WRITE` | `WriteComputeCmd(opcode=WRITE)` on `cmd_w`, then `nsamp` payload words on `data_w` | `WriteResp` on `resp_w` | puts the payload at `waddr` |
| `COMPUTE` | `WriteComputeCmd(opcode=COMPUTE)` on `cmd_w`, and **no payload** | `WriteResp` on `resp_w` | rewrites `[waddr, waddr+nsamp)` in place as `x*3 + 1` |
| `READ` | `ReadCmd` on `cmd_r` | `nsamp` words on `data_r`, then `ReadResp` on `resp_r` | returns `[raddr, raddr+nsamp)` |

`WRITE` and `COMPUTE` share a stream and a task because they share a port; `READ` is a second task on
the memory's other port. That is the point of the example — one memory, reached three ways — and it
is why the pair of `WRITE` and `COMPUTE` is worth reading as a **controlled experiment** rather than
as two features. Same task, same port, same words: only the access shape differs, and
[reading the trace](timing.md) is where that difference shows up as a number.

**Every one of the three answers, and each for its own reason.**

- A **write has no return path.** A command that does not fully land completes silently and leaves
  the memory half-written. `resp_w` is the only channel that can say otherwise.
- A **compute has no return path either**, and less of one: it produces no data at all, so without a
  response a refused `COMPUTE` and a completed one look identical from outside.
- A **refused read returns zero words**, and zero words is indistinguishable from *"not yet"* on a
  stream. A consumer waiting for `n` words that will never arrive does not see an error; it sees a
  stream that has gone quiet. The channel that reports the refusal therefore has to be one that
  answers whether or not there is data — which is exactly what the data stream cannot be.

There are **two** statuses and no more: `BramStatus.OK` and `BramStatus.OUT_OF_RANGE`. A range that
leaves the memory (`p + n > depth`) is refused **whole** — not clipped, not wrapped — because a
silent wrap hands back plausible data from the wrong place, and the refusal reaches a `COMPUTE`
exactly as it reaches a `WRITE`.

`tid` is echoed on every response, which is what lets a caller match a reply to the command it issued
instead of inferring it from the order the replies arrive in — the same reason the RF transmit
stream's response carries one.

> **The payload asymmetry is the one a caller can get wrong silently.** A refused `WRITE` *still
> consumes its payload*: the payload belongs to the command, and leaving it in the stream would shift
> every later command's data by `nsamp` words and turn one caller error into a corrupted run. A
> `COMPUTE` consumes **none** — refused or not — because it reads the words it is about to rewrite.
> Get that backwards in either direction and every command behind the first `COMPUTE` reads somebody
> else's data, with every response still saying `OK`.

> A third status — a legal range whose payload arrives short — has no scenario here and is
> deliberately absent. An unexercised branch in a teaching example is a branch the reader has to take
> on trust.

**The range check would not have caught the addressing bug**, and it is worth keeping the two apart.
The check is in **words**, the caller's units. The byte/word scaling defect lived *below* it, in the
wrapper: a command reading words 0…255 of 1024 passes the range check and still aliases. Two
different failures, two different guards — the range check is the caller's, and
[the addressing convention](../../guide/interface/primitive/bram.md#the-addressing-convention) is the other's.

## The messages

The command/response protocol needs four messages and one opcode:

| message | fields | words at 64 bits |
|---|---|---|
| `WriteComputeCmd` | `tid`, `opcode`, `nsamp`, `waddr` | 4 |
| `WriteResp` | `tid`, `status` | 2 |
| `ReadCmd` | `tid`, `nsamp`, `raddr` | 3 |
| `ReadResp` | `tid`, `status` | 2 |

As usual, the messages are defined as [`DataList` data schemas](../../guide/schema/python/datalists.md).
For example, `WriteComputeCmd` and `WriteResp` are given by:

```python
class WriteComputeCmd(DataList):
    include_filename: ClassVar[str | None] = "bram_write_compute_cmd.h"
    elements: ClassVar[dict] = {
        "tid":    {"schema": Word64, "description": "transaction id, echoed on the response"},
        "opcode": {"schema": BramOpField, "description": "WRITE (payload in) or COMPUTE (in place)"},
        "nsamp":  {"schema": Word64,
                   "description": "extent in words; payload words for WRITE, none for COMPUTE"},
        "waddr":  {"schema": Word64, "description": "first word address touched"},
    }


class WriteResp(DataList):
    include_filename: ClassVar[str | None] = "bram_write_resp.h"
    elements: ClassVar[dict] = {
        "tid":    {"schema": Word64, "description": "the command's transaction id"},
        "status": {"schema": BramStatusField, "description": "OK or OUT_OF_RANGE"},
    }
```

The `include_filename` indicates the location of the generated include file that will be discussed in
the [codegen section](./codegen.md).

**Two fields are enums, and both are `EnumField`s over a Python `IntEnum`:**

```python
class BramOp(IntEnum):
    WRITE = 0
    COMPUTE = 1


class BramStatus(IntEnum):
    OK = 0
    OUT_OF_RANGE = 1


BramOpField = EnumField.specialize(enum_type=BramOp, bitwidth=WORD_BW)
BramStatusField = EnumField.specialize(enum_type=BramStatus, bitwidth=WORD_BW)
```

Both fields are listed in `SCHEMA_CLASSES` in their own right, so each reaches C++ as a real
`enum class` and the kernel compares against a **name**:

```cpp
if (c.opcode == BramOp::WRITE) { ... }
r.status = ok ? BramStatus::OK : BramStatus::OUT_OF_RANGE;
```

A bare integer there would author the encoding a second time, in the one place nothing checks it
against the schema. `FirOpField` in `examples/fir_block` is the same pattern.

One field per stream word is a **choice this design makes, not a rule Waveflow imposes**: a command
is then exactly its field count in words, which is easy to read off a waveform. Any field widths are
possible. Waveflow serializes and deserializes whatever layout you declare — several narrow fields
pack into one word, and a field wider than the stream word spans as many words as it needs — and
because `get(WriteComputeCmd)` deserializes for you, no body ever picks the fields back out by hand.

No field ever straddles a word: fields pack in while they fit, and one that will not fit the remainder
starts a new word. The single real restriction is that an `EnumField` may not be **wider** than one
word, where an ordinary field may be — a 96-bit `IntField` on a 64-bit stream is simply two words. That
is what pins *these* messages to 64 bits: `BramOpField` is 64 bits wide, so on a 32-bit stream it is
refused outright rather than split across two.

> **Declaring the `DataList` is not enough.** The failure this design was changed to remove is
> declaring it and then unpacking it by hand anyway — `wp = yield from _word(...)` twice. That
> re-authors the field layout in the one place nothing checks it against the **generated C++ header
> the kernel compiles against**, which is the same defect as hand-rolled element packing, one level
> up.

## The two task bodies

As shown in the diagram in the [introduction](./index.md), the kernel is composed of two `HwModule`s
— a `BramWriteCompute` that serves `WRITE` and `COMPUTE`, and a `BramReadCmd` that serves `READ` —
plus the BRAM itself, which is not a task at all.

`BramWriteCompute` uses standard stream interfaces for the command, response and data, along with a
[`BramIFMaster`](../../guide/interface/primitive/bram.md) for the memory:

```python
class BramWriteCompute(FreeRunMod):
    def __post_init__(self) -> None:
        self.cmd_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
        self.data_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_data", bitwidth=w)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w",
                                  element_type=word_element(w), nelem=d, access="readwrite")
        self.resp_w = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w)
        self.go_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
```

`access="readwrite"` is the whole of what makes `COMPUTE` possible, and it is not free: it puts
`storage_type=ram_1p` on the port's pragma, which pins Vitis to **one** physical memory port — the
one the wrapper actually wires. [The `access` / `storage_type`
derivation](../../guide/interface/primitive/bram.md#accessreadwrite-and-the-storage_type-that-follows) has the
measurement.

The body dispatches on the opcode:

```python
cmd = yield from self.cmd_w.get(WriteComputeCmd)
wp, n, op = int(cmd.waddr), int(cmd.nsamp), BramOp(int(cmd.opcode))
ok = n <= int(self.depth) and wp <= int(self.depth) - n
if op is BramOp.WRITE:
    if n:
        x, tstart = yield from self.data_w.get_pipelined(self.buf_w.element_type, count=n)
        if ok:                               # refused: consumed, then dropped on the floor
            yield from self.buf_w.write_pipelined(x, wp, tstart)
elif ok and n:
    x = self.buf_w.array_ref(wp, n)
    x[:] = x * 3 + 1
    yield self.timeout(n * self.buf_w.ii_for(2) / float(self.clk.freq))
resp = WriteResp()
resp.tid = cmd.tid
resp.status = BramStatus.OK if ok else BramStatus.OUT_OF_RANGE
yield from self.resp_w.write(resp)
```

**There is no `for` in it, and that is the point.** A per-element loop in a pysim body opts the design
out of the LT model that is the tool's reason to exist; the C++ keeps its
`#pragma HLS PIPELINE II=1` loop, exactly as `poly_evaluate_impl.tpp` keeps its lane loop. Three
things carry the work instead:

- **The command is read in one call and the response written in one.** That is the
  [fourth row of the four ways to move data](../../guide/interface/primitive/stream.md#the-four-ways-to-move-data):
  `get(WriteComputeCmd)` derives the word count from `WriteComputeCmd.nwords_per_inst(bitwidth)` and
  deserializes; `write(resp)` serializes. Neither end counts words.
- **The `WRITE` payload is one vector in and one vector out.** `get_pipelined` returns the whole
  payload *and* the cycle its first word arrived; passing that cycle to `write_pipelined` as
  `t_start` says the memory write began then, so the two phases **overlap** and the pair costs
  `max(stream, memory)` rather than their sum — which is what a task writing a word as it receives it
  actually does.
- **The `COMPUTE` is one numpy expression over a live view.** `array_ref` returns a view of the
  memory's own storage, so `x*3 + 1` *is* the computation; nothing is transferred, which is exactly
  why nothing about it costs cycles by itself. Routing it through a read, a compute and a write would
  invent two transfers that do not exist and charge the design for them.

The `COMPUTE` branch is the one place a body charges its own time, and even there the **number** is
not written down: `ii_for(2)` asks the port what two accesses per element cost, and the port answers
2 because `ram_1p` gave it one physical port. See
[the three access cases](../../guide/interface/overview.md#the-three-access-cases) for why in-place
work owns its timing while a transfer does not.

Two more details that are not style:

- **`ok` is spelled `n <= N and wp <= N - n`, never `wp + n <= N`.** In the C++ twin the operands are
  unsigned, and the sum of two legal-looking values wraps — turning an out-of-range command into an
  accepted one at exactly the widths where a memory is most likely to be full. Neither term of the
  spelling used can overflow.
- **The `if` on the opcode is what keeps the two streams in step.** It is not a tidy dispatch; see
  the payload asymmetry above.

### The read task

The mirror image, plus the arming:

```python
if not self.armed:
    yield from _word(self.go_in)
    self.armed = True
cmd = yield from self.cmd_r.get(ReadCmd)
rp, n = int(cmd.raddr), int(cmd.nsamp)
ok = n <= int(self.depth) and rp <= int(self.depth) - n
if ok and n:
    y, tstart = yield from self.buf_r.read_pipelined(self.buf_r.element_type, n, rp)
    yield from self.data_r.write_pipelined(y, tstart)
```

One token, consumed once, and then the reader is command-driven forever —
[sequencing belongs in the design](../../guide/interface/primitive/bram.md#sequencing-belongs-in-the-design)
explains why it is a token on an ordinary stream rather than a testbench's ordering.

`read_pipelined` is where the read path's cost lives, and it is worth reading slowly. A scalar
`BramIF` access is **untimed in pysim, on purpose**: a BRAM answer is deterministic, unarbitrated and
one cycle, so a discrete-event model of it would add a SimPy timestep and no fidelity — `read`
and `write` are plain methods rather than generators, and **the absence of the `yield` is the
interface stating that no time passes**. (Contrast [AXI-MM](../../guide/interface/primitive/aximm.md), where the
bus, the arbitration and the burst *are* the point of having a model.)

What that leaves out is not throughput. At II=1 a pipelined reader still answers one word per cycle
whatever the memory's latency is — the pipeline hides it. What it leaves out is **when the first
answer appears**, which at RTL is `READ_LATENCY` cycles after the address. So `read_pipelined`
publishes the model: `READ_LATENCY` cycles of fill, then one element per cycle, with the fill paid
once per transfer because it *is* a pipeline fill.

**The number is never written down in Python.** `BramIFMaster.read_latency` resolves through the
bound `BramIF` to the memory module, which reads it out of the Verilog's `localparam READ_LATENCY`. A
student writing `yield self.timeout(1)` with a hard-coded `1` is doing exactly the thing the
framework refuses to do — and the framework refuses it by **raising when the port is unbound**:

```python
from waveflow.build.elaborate import ElabContext
from waveflow.hw.bram import BramIFMaster, word_element

loose = BramIFMaster(sim=ElabContext(), name="loose",
                     element_type=word_element(64), nelem=1024, access="read")
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
from examples.bram_access.bram_access import BramAccess

comp = elaborate(BramAccess, {"bitwidth": 64, "depth": 1024}, name="bram_access")
print(comp.rd.buf_r.read_latency, comp.mem.read_latency)
```

```
1 1
```

### The kernel reads the same messages the same way

The hand-written task bodies are the C++ half of the same idiom, and they are worth reading beside
the Python:

```cpp
    WriteComputeCmd c;
    c.read_stream<W>(cmd);
    bool ok = bram_cmd_in_range<W, N>(c.waddr, c.nsamp);
```

```cpp
    WriteResp r;
    r.tid = c.tid;
    r.status = ok ? BramStatus::OK : BramStatus::OUT_OF_RANGE;
    r.write_stream<W>(resp);
```

`read_stream` and `write_stream` come from `bram_write_compute_cmd.h` and `bram_write_resp.h` — the
headers the schemas generate — so the kernel and the model cannot disagree about the layout. The
[kernel transfer reference](../../guide/custom_hooks/reference.md#mapping-the-python-transfer-interfaces-to-the-kernel)
maps every Python call to its HLS twin:

| Python | HLS |
|---|---|
| `get(Schema)` | `Schema c; c.read_stream<W>(s);` — **one call, never `n` × `s.read()`** |
| `write(obj)` | `obj.write_stream<W>(s)` |
| `get(nwords_max=1)` | `s.read()` — the payload, and only the payload |

What is **not** generated is the range check. That is a piece of the design's *logic* rather than of
its message layout, so it stays hand-written in `src/bram_cmd_range.h` — layout is the thing that must
have exactly one author. Neither is `array_ref`: it has no HLS lowering, because in C++ the port
simply *is* the array and reading and writing it through one subscript is what "in place" means. The
`COMPUTE` branch of the task body is therefore hand-written too:

```cpp
compute_inplace:
        for (ap_uint<32> i = 0; i < c.nsamp; i++) {
#pragma HLS PIPELINE II=1
            buf_w[c.waddr + i] = buf_w[c.waddr + i] * 3 + 1;
        }
```

The pragma asks for II=1 and Vitis schedules it at **2**, because `ram_1p` gave it one port and a
read-modify-write is two accesses per element. That is not a missed target; it is the price the
declaration bought, and [reading the trace](timing.md) measures it.

## The top level, and the memory beside it

The registrations *are* the design:

```python
self.add_comp(self.wr)
self.add_comp(self.rd)

go_if = StreamIF(name=f"{self.name}_go_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
go_if.bind(ep_name="master", endpoint=self.wr.go_out)
go_if.bind(ep_name="slave", endpoint=self.rd.go_in)
self.add_if(go_if)

self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                   element_type=word_element(w), nelem=d,
                   port_access=("readwrite", "read"))
self.add_rtl_mod(self.mem)
w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim, clk=self.clk)
w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
self.add_rtl_if(w_if)
```

`add_rtl_mod` and `add_rtl_if` are different registries from `add_comp` and `add_if`, and
[that difference is the mechanism](../../guide/interface/primitive/bram.md#add_rtl_if-not-add_if--and-that-is-the-whole-mechanism):
the walks that derive tasks and channels read the *other* two, so a memory is never asked for a
`kernel_task()` it does not have, and the accessor's port never vanishes into a FIFO. The result is
visible on the elaborated graph:

```python
from waveflow.build.elaborate import elaborate
from examples.bram_access.bram_access import BramAccess

comp = elaborate(BramAccess, {"bitwidth": 64, "depth": 1024}, name="bram_access")
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

**`port_access` has to agree with the accessor**, because `BramIF.bind` requires the two `access`
declarations to be *identical*: they are two statements of one fact. Port A is `"readwrite"` here
because the task bound to it is; port B stays read-only, and that is not a preference —
`bram_t2p.v`'s `$error` is written one-sided, so a writing port B would be invisible to the design's
only real check, and `T2pBram` refuses it at construction.

**`mem`, not `buf`.** The attribute name becomes the Verilog *instance* name, and `buf` is a Verilog
primitive gate — `bram_t2p #(...) buf (...)` is a syntax error. The wrapper emitter refuses reserved
names by name, rather than letting `xvlog` fail on something that mentions no Python.

### The memory module

`T2pBram` is framework, in [`waveflow/hw/bram.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/hw/bram.py).
Three of its properties are worth knowing:

```python
from waveflow.hw.bram import T2pBram, ramb18_count, word_element
from waveflow.build.elaborate import ElabContext

mem = T2pBram(sim=ElabContext(), name="mem", element_type=word_element(64), nelem=1024)
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
  outside it. See [code generation](codegen.md).

The memory is **element-typed**: `element_type` plus `nelem`, with `bitwidth` derived from the
element rather than declared beside it. That is what makes `array_ref` a real view — the pysim
storage is `np.zeros(nelem, dtype=<the element's dtype>)`, so a stored value is readable as itself
and a write through the reference lands in the memory. `store` / `load` **refuse an out-of-range
address** rather than wrapping it, because the RTL wraps silently and a silent wrap is the bug worth
catching early.

## See also

- [Python simulation](pysim.md) — running this model, and the scenario it runs.
- [BRAM — memory between modules](../../guide/interface/primitive/bram.md) — the interface reference: why the
  memory cannot live inside a kernel, the `access` / `storage_type` derivation, the addressing
  convention, and the `$error` nothing can hear.
- [The three access cases](../../guide/interface/overview.md#the-three-access-cases) — the frame the
  three transactions sit in.
- [A module realized as Verilog](../../guide/comp_codegen/rtl_module.md) — `rtl_module()`, and the
  latency single-source rule this page relies on.
