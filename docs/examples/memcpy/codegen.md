---
title: Kernel codegen
parent: Memory Copy
nav_order: 5
---

# Kernel codegen

The [Python model](./python.md) is a graph: three leaves, two framed edges, four boundary ports.
Generating the kernel is walking that graph. `composite_top_spec` reads the sub-components' `kernel_task()`
signatures and the `add_if` edges, resolves each task argument to either a boundary port or an internal
FIFO, and `render_top` emits the `ap_ctrl_none` top — one `hls::task` per child, one FIFO per edge, the
boundary pragmas from the `boundary` list. Nothing about the top is hand-written; it *is* the graph made
concrete:

```cpp
// examples/mem_copy/gen/mem_copy.cpp — GENERATED
void mem_copy(hls::stream<ap_uint<64> >& s_cmd, const ap_uint<64>* m_in,
              ap_uint<64>* m_out, hls::stream<ap_uint<64> >& s_done) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE m_axi port=m_in  offset=slave bundle=gmem0 depth=8192
#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1 depth=8192
#pragma HLS INTERFACE axis port=s_done
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<streamutils::framed_word<64> > cmd;         // Sequencer -> reader
    hls_thread_local hls::stream<streamutils::framed_word<64> > copy_data;   // reader -> writer
    hls_thread_local hls::task t0(mem_seq_framed_task<64>, s_cmd, cmd);
    hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd, m_in, copy_data);
    hls_thread_local hls::task t2(mem_w_stream_framed_done_task<64, 8>, copy_data, m_out, s_done);
}
```

Each edge is a `framed_word` FIFO (a `StreamIF(framed=True)`); each `m_axi` bundle is assigned by policy
in boundary order (`m_in`→`gmem0`, `m_out`→`gmem1`); the writer's `max_xfer_len=8` rides along as a
second template argument. There is no `#define` and no `while`: the width is a template argument and the
`hls::task` runtime re-fires each single-firing body per job.

## The bodies are all hand-written

The top generates; **every task body here does not**. `MemStreamStep` copies the three fixed headers —
`mem_seq_framed_task.h`, `mem_r_stream_framed_task.h`, `mem_w_stream_framed_done_task.h` — verbatim into
`include/`. Two reasons put a body on the hand-written side of the line:

- **it owns `m_axi`** — the mem-stream read/write bodies do, and task-body emission refuses a memory
  port (bundle naming, depth, and the offset register are decisions the emitter does not make); or
- **it constructs descriptors / drives a framed channel** — the `Sequencer` does both, neither of which
  is in the code generator's vocabulary.

So for `mem_copy` the rule is simply: *the top generates, the bodies are copied.* Each body's `run_iter`
([Python model](./python.md)) is a **pysim golden** whose only tie to its C++ is a test — the pysim
golden on the Python side, [XSI](./testbench.md) on the RTL side. Drift is silent, so the tests are the
contract.

> **Task-body *generation* still exists.** A *stream-only* leaf whose `run_iter` is just `get` →
> `@synthesizable` hook → `write` can have its body **generated** from `run_iter` by `TaskBodyStep` — the
> FIR pipeline is built that way. `mem_copy` simply has no such leaf; its Sequencer was retired from a
> generated two-stream body to a hand-written framed one when the in-band forwarding protocol replaced
> the two-stream one (`plans/memcopy_inband_integration.md`).

Anything *generated* — the top, the port binding, the testbench's command words — cannot drift by
construction, which is the point of generating it. The banner on each says so; a regenerate overwrites
a hand-edit.
