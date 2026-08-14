---
title: BRAM — memory between modules
parent: Interfaces
nav_order: 3.5
audience: python
applies_to: [BramIF, BramIFMaster, BramIFSlave, T2pBram]
summary: "BramIF connects a kernel task to an on-chip memory that lives OUTSIDE the kernel, as hand-written Verilog joined by a generated wrapper. Explains why a memory shared between two tasks cannot live inside a Vitis kernel — with the PIPO and dataflow-check evidence — and why a BramIF is registered with add_rtl_if rather than add_if, which is what keeps the accessor's port a boundary port."
---

# BRAM — memory between modules

A `BramIF` connects one kernel task to one port of an on-chip memory. It is the only interface in
this section whose far end is **not** inside the generated kernel: the memory is hand-written Verilog
instantiated beside it, and a generated wrapper joins the two.

That is not a design preference. It is the only structure Vitis leaves available.

## Why a shared memory cannot live inside a kernel

Two `hls::task` bodies sharing a buffer is the natural way to write a capture buffer, a scoreboard, a
reorder queue. Two constructs look like they would express it. Both were measured — Vitis HLS 2025.1,
`xczu48dr-ffvg1517-2-e`; the full experiment log is in
[`plans/rtl_module.md`](../../../plans/rtl_module.md).

### A local array shared between two tasks — **compiles, and means something else**

```
INFO: [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
```

```verilog
assign read_task_U0_ap_start     = buf_r_t_empty_n;   // reader gated on release
assign write_task_U0_ap_continue = buf_r_i_full_n;    // WRITER STALLS when full
```

This is the dangerous one, and it is why "it csynthed" is not evidence of anything. The C++ reads
like a circular buffer; the RTL is a synchronized ping-pong. The writer **stalls** when the consumer
has not released a buffer — and a stage facing a converter, an ADC, or anything else that cannot wait
may never stall.

### One `bram` port written by one task and read by the other — **hard error**

```
ERROR: [HLS 200-976] Argument 'buf_r' failed dataflow checking:
                     Cannot read as well as write over function parameter.
```

### Why the tool is not being obtuse

The objection is **dataflow channel** semantics, not arbitration. `DATAFLOW`'s promise is that the
parallel result equals the sequential C result — and a shared buffer with independent pointers has no
sequential-C meaning at all. Whether `buf[rd]` sees the old value or the new one depends on *when*,
which C does not express.

So the real division is **who owns the correctness argument**. For a channel, the tool owns it and
enforces it with handshakes. For `m_axi`, and for this interface, the *designer* owns it and the tool
does not interfere. A local array is inside the tool's analysis scope by construction, so it always
gets channel treatment. There is no third option *inside* a kernel — which is what puts the memory
outside one.

## The shape

```python
self.buf_w = BramIFMaster(bitwidth=16, depth=1024, access="write")   # on the accessor task
self.mem   = T2pBram(dwidth=16, depth=1024)                          # the memory module
...
self.add_rtl_mod(self.mem)          # realized as hand-written Verilog beside the kernel
w_if = BramIF(name="bufw_if", sim=self.sim)
w_if.bind(ep_name="master", endpoint=self.wr.buf_w)
w_if.bind(ep_name="slave",  endpoint=self.mem.wr_port)
self.add_rtl_if(w_if)               # a WRAPPER WIRE, not an internal channel
```

* **`BramIFMaster`** is the accessor's end — a kernel task's window onto storage it does not own. In
  C++ it is a *sized array parameter* (`ap_uint<16> buf_w[1024]`) carrying
  `#pragma HLS INTERFACE mode=bram`; in RTL it is **fourteen** ports, an A/B pair of seven signals.
* **`BramIFSlave`** is the memory's end. One endpoint is one *port* of the memory; a `T2pBram` has
  two.
* **`access`** (`"read"` / `"write"`) is declared on both ends and checked when they bind. A port
  used both ways is what Vitis refuses inside a kernel, and it is no safer outside one.

## `add_rtl_if`, not `add_if` — and that is the whole mechanism

A [`StreamIF`](./stream.md) registered with `add_if` is an **internal channel**: it lowers to an
`hls::stream` inside the generated top, and both its endpoints leave the top's boundary. A `BramIF`
is not that. One end is inside the kernel and the other is outside it, so the accessor's end **must
stay a boundary port** and the join must happen one level up.

Because `add_rtl_if` is a different registry, `derive_boundary` — which reads the `add_if` one —
never sees a `BramIF` at all. The accessor's port is therefore an unbound child endpoint, and the
existing rule (*a child endpoint not bound to an internal interface **is** a boundary port*) makes it
a port of the kernel with **no change to that walk**. Putting a `BramIF` in `add_if` would instead
make the kernel's memory ports vanish into a FIFO that does not exist.

The memory itself is registered with `add_rtl_mod` for the mirror-image reason: it is not a task, so
no walk that emits `hls::task`s should ever meet it.

## What the two backends model

| | pysim | RTL |
|---|---|---|
| storage | a numpy array on the memory module | the hand-written `.v` |
| access | **untimed** — a plain method call | one cycle, from the memory's published `READ_LATENCY` |
| ordering | whatever the graph does | the same, and the memory `$error`s if the reader touches the address being written |

The access is untimed in pysim on purpose: a BRAM answer is deterministic, unarbitrated and
one cycle, so a discrete-event model of it would add a timestep and no fidelity. Contrast
[AXI-MM](./aximm.md), where the bus, the arbitration and the burst *are* the point of having a model.

**The correctness argument is yours, and the memory helps you keep it.** `bram_t2p.v` `$error`s when
port B reads the address port A is writing that cycle — for a circular buffer, *rd trails wr*.
Nothing else would check it: if it fails, the data is whatever the BRAM's read-during-write mode
happens to be, and no tool says a word. A hand-written memory is *more* verifiable than an emulated
one.

## Sequencing belongs in the design

`examples/bram_toy` is the worked example, and it makes the point by having had to solve it. Its
writer emits one "buffer ready" token on an ordinary internal stream; its reader waits for that token
once, then serves addresses. The witness this example reproduces got the same ordering from its
*testbench* (drive all 256 samples, then the addresses) — which a concurrent BFM harness cannot do,
because both drivers push from cycle 0. If your reader must not overtake your writer, say so with a
channel; do not rely on a testbench's habits.

## See also

- [A module realized as Verilog](../comp_codegen/rtl_module.md) — the `rtl_module()` hook the memory
  declares, the port-name chain, and the latency single-source rule.
- [Free-running composite](../comp_codegen/freerunning_composite.md) — where the wrapper fits, and
  what `csynth` does *not* count.
- [Memory](../memory/) — the other storage categories, and which of them the tool chooses for you.
