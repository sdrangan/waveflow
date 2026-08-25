---
title: Python model
parent: Shared memory between two modules
nav_order: 2
has_children: false
---

# Python model

The [overview](overview.md) covered *why* the memory is outside the kernel. This page writes the
Python that says *how*: two tasks, the memory, the interfaces between them, and the one place the
model has to pay a cost the hardware charges.

Everything is in [`examples/bram_simple/bram_simple.py`](https://github.com/sdrangan/waveflow/tree/main/examples/bram_simple/bram_simple.py).

## The write task

A `FreeRunMod` declares its endpoints in `__post_init__`. Four of the five here are ordinary streams;
the fifth is the memory port.

```python
self.cmd_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd", bitwidth=w)
self.data_w = StreamIFSlave(sim=self.sim, name=f"{self.name}_data", bitwidth=w)
self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w, depth=d,
                          access="write")
self.resp_w = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w)
self.go_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_go", bitwidth=w)
```

`BramIFMaster` is the **accessor's** end — a task's window onto storage it does not own. Two of its
arguments are load-bearing:

- **`depth`** becomes the C++ array's *size*. `mode=bram` on an unsized pointer degrades to an
  `ap_vld` scalar port **silently**: no warning, a clean `csynth`, and a design elaborated against a
  memory that is not there. The size is what makes the pragma take effect.
- **`access`** is declared, never inferred, and is checked when the interface binds. A port used both
  ways is what Vitis refuses inside a kernel, and it is no safer outside one.

The body is one command per firing:

```python
wp = yield from _word(self.cmd_w)
n = yield from _word(self.cmd_w)
ok = n <= int(self.depth) and wp <= int(self.depth) - n
for i in range(n):
    x = yield from _word(self.data_w)
    if ok:                                   # refused: consumed, then dropped on the floor
        self.buf_w.mem_write(wp + i, x)
yield from self.resp_w.write(
    np.array([ST_OK if ok else ST_OUT_OF_RANGE], dtype=np.uint64))
```

Two details that are not style:

- **`ok` is spelled `n <= N and wp <= N - n`, never `wp + n <= N`.** In the C++ twin the operands are
  `ap_uint<W>`, and at 16 bits with a 1024-word memory the sum of two legal-looking values wraps —
  turning an out-of-range command into an accepted one at exactly the widths where a memory is most
  likely to be full. Neither term of the spelling used can overflow.
- **A refused command still consumes its payload.** The payload belongs to the command; leaving it in
  the stream would shift every later command's data by `n` words and turn one caller error into a
  corrupted run.

`mem_write` is a **plain method call**, not a generator. That is the interface saying no simulated
time passes — see [the read path](#the-read-path-is-the-one-that-costs-something) below.

## The read task

The mirror image, plus the arming:

```python
if not self.armed:
    yield from _word(self.go_in)
    self.armed = True
rp = yield from _word(self.cmd_r)
n = yield from _word(self.cmd_r)
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
