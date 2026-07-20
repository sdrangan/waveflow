---
title: DUT codegen
parent: Memory Copy
nav_order: 4
---

# DUT codegen — the graph becomes an `hls::task` top

The [Python model](./python.md) is a graph: three leaves, two framed edges, four boundary ports.
Generating the **DUT** kernel is walking that graph and lowering it to a free-running Vitis HLS top.

## What an `hls::task` is

Vitis HLS `hls::task` is the free-running execution model. A task is a body that the runtime **re-fires
on its own** whenever its input streams have data — there is no host `start`/`done` handshake, so the
top is declared `ap_ctrl_none`. Tasks connected by `hls::stream` FIFOs run **concurrently** and
**overlap**: one task processes job *j+1* while another is still on job *j*. That is exactly the
[concurrent flow](../../guide/flows/concurrent.md)'s model, and it is why `mem_copy`'s three stages —
sequence, read, write — become three tasks wired by FIFOs rather than one sequential function.

The contrast is `ap_ctrl_hs` (the [sequential flow](../../guide/flows/sequential.md)): a kernel the
host launches once and waits on. A free-running task cannot be driven that way — which is also why it
cannot be verified by Vitis C/RTL cosim, and is instead run through [XSI](./rtlsim.md).

## How a `FreeRunComp` lowers to a task top

Each leaf `FreeRunComp` becomes one `hls::task`; each `add_if` edge becomes one `hls_thread_local`
FIFO; the `boundary` list becomes the interface pragmas. `composite_top_spec` reads the
sub-components' `kernel_task()` signatures and the edges, resolves each task argument to a boundary port
or an internal FIFO, and `render_top` emits it. Nothing about the top is hand-written — it *is* the
graph made concrete:

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

Each edge is a `framed_word` FIFO (a `StreamIF(framed=True)`); each `m_axi` bundle is assigned by
policy in boundary order (`m_in`→`gmem0`, `m_out`→`gmem1`); the writer's `max_fwd_words=8` rides along
as a second template argument. There is no `#define` and no `while`: the width is a template argument,
and the task runtime supplies the re-firing.

## The bodies are all hand-written

The top generates; **every task body does not**. `MemStreamStep` copies the three fixed headers
(`mem_seq_framed_task.h`, `mem_r_stream_framed_task.h`, `mem_w_stream_framed_done_task.h`) verbatim into
`include/`. Two reasons put a body on the hand-written side of the line:

- **it owns `m_axi`** — the mem-stream read/write bodies do, and task-body emission refuses a memory
  port (bundle naming, depth, and the offset register are decisions the emitter does not make); or
- **it constructs descriptors / drives a framed channel** — the `Sequencer` does both, neither of
  which is in the code generator's vocabulary.

So for `mem_copy` the rule is simply: *the top generates, the bodies are copied.* Each body's
`run_iter` ([Python model](./python.md)) is a **pysim golden** whose only tie to its C++ is a test.

> **Task-body *generation* still exists.** A *stream-only* leaf whose `run_iter` is just `get` →
> `@synthesizable` hook → `write` can have its body **generated** from `run_iter` by `TaskBodyStep` —
> the FIR pipeline is built that way. `mem_copy` simply has no such leaf.

## Building it

`codegen_dut` is the build step that emits all of this — the top, its csynth `.tcl`, the port map, and
the headers:

```bash
python examples/mem_copy/mem_copy_build.py --through codegen_dut
```

```
codegen_dut:
    gen\mem_copy.cpp
    mem_copy.tcl
    xsi\mem_copy_ports.h
    RUNNING...
generated DUT gen\mem_copy.cpp + mem_copy.tcl + xsi/mem_copy_ports.h
    PASSED
```

`gen/mem_copy.cpp` is the top above; `mem_copy.tcl` drives Vitis HLS C-synthesis (the `csynth` step,
which needs Vitis and produces the RTL the [RTL rung](./rtlsim.md) drives); `xsi/mem_copy_ports.h` is
the DUT's port map, which the generated [testbench harness](./codegen_tb.md) includes. Anything
generated carries a banner and a regenerate overwrites a hand-edit — so don't hand-edit them.

## Next

[Testbench codegen](./codegen_tb.md) — how the `MemCopyTB` graph becomes the XSI BFM harness that
drives this top.
