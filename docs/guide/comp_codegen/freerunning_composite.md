---
title: Free-running composite in HLS
parent: Module Code Generation
nav_order: 5
audience: hls
applies_to: [FreeRunMod]
api: [composite_top_spec, render_top, derive_boundary, derive_internal_edges, StreamEdge, SobEdge]
summary: "The same composite_kernel target with more than one task. Walks mem_copy (Sequencer -> MemRStream -> MemWStream) from graph to generated top: one hls_thread_local channel per internal interface, one hls::task per child, boundary ports derived from which endpoints were left unbound, and the ownership rule about which task may hold m_axi."
---

# Free-running composite in HLS

A composite is a [`FreeRunMod`](../flows/concurrent.md) with **sub-components instead of a body**. It
lowers to the same `composite_kernel` target as [a leaf](./freerunning.md) — the difference is only
that the graph has more than one node, so the generated top has more than one `hls::task` in it and
some channels between them.

Nothing new is extracted here. The children's bodies are their own artifacts; this page is about how
the **top** is derived from structure.

## The example

`mem_copy` — copy a run of words from one buffer to another. Three children:

```
s_cmd ──▶ Sequencer ──cmd──▶ MemRStream ──copy_data──▶ MemWStream ──▶ s_done
                                  │                         │
                                m_in                      m_out      (m_axi)
```

The Python declares only the graph ([`examples/mem_copy/mem_copy.py`](../../../examples/mem_copy/mem_copy.py)):

```python
self.seq     = Sequencer(name=..., mem_dwidth=w, clk=self.clk)
self.rstream = MemRStream(name=..., mem_dwidth=w, inband=True, clk=self.clk)
self.wstream = MemWStream(name=..., mem_dwidth=w, emit_done=True, inband=True, clk=self.clk)
for c in (self.seq, self.rstream, self.wstream):
    self.add_comp(c)                                  # insertion order == task emit order

self._cmd_if = StreamIF(name=..., bitwidth=w, framed=True)
self._cmd_if.bind("master", self.seq.cmd_out)
self._cmd_if.bind("slave",  self.rstream.s_cmd)
self.add_if(self._cmd_if)                             # an internal channel
# ... and the reader -> writer data edge
```

And the generated top:

```cpp
void mem_copy(
    hls::stream<ap_uint<64> >& s_cmd,
    const ap_uint<64>* m_in,
    ap_uint<64>* m_out,
    hls::stream<ap_uint<64> >& s_done
) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0 depth=8192
#pragma HLS stable variable=m_in
#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1 depth=8192
#pragma HLS INTERFACE axis port=s_done
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<streamutils::framed_word<64> > cmd;
    hls_thread_local hls::stream<streamutils::framed_word<64> > copy_data;
    hls_thread_local hls::task t0(mem_seq_framed_task<64>, s_cmd, cmd);
    hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd, m_in, copy_data);
    hls_thread_local hls::task t2(mem_w_stream_framed_done_task<64, 8>, copy_data, m_out, s_done);
}
```

Four things were derived. None of them was declared twice.

## 1. `add_if` became the channels

Each internal interface became one `hls_thread_local` declaration. The **kind** came from the
interface's type, not from a tag beside it:

| Python interface | C++ channel |
|---|---|
| `StreamIF(framed=True)` | `hls::stream<streamutils::framed_word<W> >` — a FIFO carrying packet boundaries |
| `StreamIF` | `hls::stream<ap_uint<W> >` — a plain word FIFO |
| `StreamOfBlocksIF` | `hls::stream_of_blocks<T[N], depth>` — a ping-pong block channel |

A FIFO's `depth` is a physical property of the `StreamIF`, single-sourced so the pysim queue and the
emitted pragma cannot disagree. A `StreamIF` with `depth=None` is *explicit unbounded* — fine for
pysim exploration, refused at the synthesis boundary, because an unbounded FIFO is not hardware.

## 2. `add_comp` became the tasks

One `hls::task` per child, in `add_comp` insertion order. Each child named its body with a
[`KernelTask`](./freerunning.md#kerneltask-and-the-hand-off), and the generator resolved that
signature's endpoint names to concrete call arguments — each one either a boundary port or one of the
channels above.

That resolution is the whole trick. `mem_r_stream_framed_task<64>` is called with `cmd, m_in,
copy_data`: its first argument resolved to an internal channel, its second to a top-level port, its
third to another internal channel — decided by *which interfaces the endpoints were bound to*, not by
anything the child said about itself. The same `MemRStream` drops into a different graph and gets
different arguments.

## 3. The boundary was what was left over

A composite declares only the **names** of its boundary ports:

```python
self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
```

The endpoints and their order are derived: **a child endpoint not bound to one of the composite's
internal interfaces *is* a boundary port**. Only names need declaring because local names collide —
two children may each call their AXI port `m_mem`.

Direction then comes from the endpoint's *type*. `MMIFReadMaster` became `const ap_uint<64>*`;
`MMIFWriteMaster` became a non-const pointer. A bare `MMIFMaster` is **refused** rather than
defaulted: a read+write `m_axi` is legal hardware, but guessing a direction would emit a `const`
pointer for a port that gets written.

## 4. The m_axi pragmas came from assembler policy

`bundle=gmem0` and `bundle=gmem1` are not in the Python. Bundle assignment is the *assembler's*
policy — how the ports are grouped onto AXI interfaces — kept separate from direction, which is the
endpoint's type. `offset=slave` means the pointer's base address is not a port; it arrives in an
AXI-Lite register the host writes.

Note this top carries `m_axi` **and** is `ap_ctrl_none`. That combination is fine at the top level;
the constraint that bites is one level down, in the [task bodies](./freerunning.md).

## Which task may own what

The children are not interchangeable, and the split is **structural, not stylistic**:

- a task that owns an `m_axi` master should touch streams and tokens only
- a task that holds stream-of-blocks locks should not also own `m_axi`

The interleaver follows exactly this: `il_mem_r` / `il_mem_w` own the `m_axi`, while `il_load` /
`il_compute` / `il_store` own the lock-scoped block work. Mixing the two ownerships in one task is
what the rule exists to prevent.

**Multi-master arbitration is deliberately not implemented.** The generator uses fixed ownership
boundaries — one read owner, one write owner — and explicit wiring, rather than an arbitration policy.
A design needing two masters on one resource has to say so structurally.

## The trap worth knowing before you build one

In a free-running pipeline **every stage needs a token per job**. A stage that consumes input and
emits nothing on some jobs will stall a downstream stage waiting on it, and the failure looks like a
hang rather than a wrong answer. If an opcode produces no output, forward something anyway.

This framework has hit it twice — once as an un-paced pipeline deadlocking at `done = N+1`, once as a
relay that read when it had been given zero bursts to forward.

## See also

- [Free-running kernel in HLS](./freerunning.md) — the 1-task case, and the task body itself.
- [Writing it in Python](../flows/concurrent_python.md) — declaring the graph.
- [XSI testbench](./xsi_tb.md) — how a composite top is verified at RTL.
- [Streaming Memory Kernels](../memory/memstream.md) — the `MemRStream` / `MemWStream` children used
  here.
