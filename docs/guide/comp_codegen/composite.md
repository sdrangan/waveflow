---
title: Free-running kernel in HLS
parent: Module Code Generation
nav_order: 3
audience: hls
api: [composite_top_spec, render_top, task_files_to_str, KernelTask]
summary: "How a FreeRunMod is realized as an ap_ctrl_none hls::task top: composite_top_spec walks the module/interface graph into a TopSpec, render_top emits the top (interface pragmas, hls_thread_local channels, one hls::task per child), and task_files_to_str lowers a leaf's run_iter into a templated task body carrying its HwState statics. One target, composite_kernel, covers a leaf and a composite alike — a leaf is the 1-task degenerate case."
---

# Free-running kernel in HLS

A [`FreeRunMod`](../flows/concurrent.md) lowers to an `ap_ctrl_none` top whose whole body is
`hls::task` instantiations. There is no `ap_start`, no `ap_done`, and no control loop: the runtime
**re-fires** each task on every new job, so a task body is *one firing*, not a loop over jobs.

The target is `composite_kernel`, and it covers a leaf and a composite alike — **a leaf is the 1-task
degenerate case**, walked by the same generator. There is no separate `free_running_kernel`.

## Two artifacts, generated separately

| Artifact | From | By |
|---|---|---|
| the **top** — ports, pragmas, channels, task instantiations | the module + interface *graph* | `composite_top_spec` → `render_top` |
| a **task body** — one firing | a leaf's `run_iter` | `task_files_to_str` |

They are separate because they come from different things. The top comes from **structure** — what
`add_comp` and `add_if` recorded — and never from a function body. The task body comes from a leaf's
`run_iter`, through the same [extractor](./extractor.md) the control-driven flow uses.

## The top

`composite_top_spec` walks the parent's `sub_comps` and internal `interfaces`, resolves each child's
`KernelTask` signature (endpoint attribute names, in task-argument order) to either a boundary port or
an internal channel, and hands `render_top` a `TopSpec`. For a leaf that walk finds one task and no
internal edges:

```cpp
void state_accum(
    hls::stream<ap_uint<32> >& s_in,
    hls::stream<ap_uint<32> >& m_out
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t0(state_accum_task, s_in, m_out);
}
```

and for a composite, one `hls_thread_local` channel per internal edge and one task per child:

```cpp
    hls_thread_local hls::stream<streamutils::framed_word<64> > cmd;
    hls_thread_local hls::stream<streamutils::framed_word<64> > copy_data;
    hls_thread_local hls::task t0(mem_seq_framed_task<64>, s_cmd, cmd);
    hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd, m_in, copy_data);
    hls_thread_local hls::task t2(mem_w_stream_framed_done_task<64, 8>, copy_data, m_out, s_done);
```

**Nothing here is declared twice.** Port direction comes from the endpoint's *type*
(`kind_of_endpoint`), the channel kind from the interface's type (a `StreamIF` → a FIFO, a
`StreamOfBlocksIF` → a ping-pong block channel), and the bundle from the assembler's policy. A leaf
declares no boundary at all — it is derived from `kernel_task()`'s signature, so the top's parameter
list and the task's call arguments are literally the same list and cannot disagree.

### `KernelTask`

A module hands the generator a `KernelTask` naming its body:

```python
def kernel_task(self) -> KernelTask:
    return KernelTask("state_accum_task", "state_accum_task.h", ("s_in", "m_out"),
                      template_args=())
```

`signature` is the **endpoint attribute names in task-argument order** — the seam that makes the top
graph-derived rather than hand-written. `template_args` are baked-concrete C++ template arguments;
an empty tuple emits the function name bare, which is what a generated body wants when it has already
baked its width.

## The task body

`task_files_to_str` extracts a leaf's `run_iter` and emits `<name>_task.h` — templated, `static`, and
pragma-free, because the top owns the interface:

```cpp
static void state_accum_task(
    hls::stream<ap_uint<32> >& s_in,
    hls::stream<ap_uint<32> >& m_out
) {
    // Cross-firing state (HwModule.add_state) -- persists across firings.
    static ap_uint<32> total[4];
    Vec4 x;
    x.read_stream<32>(s_in);
    Vec4 y = state_accum_impl::accumulate(x, total);
    y.write_stream<32>(m_out);
}
```

Three things to read off it:

**No command loop.** One invocation is one firing. The `while True:` in the Python is the
discrete-event stand-in for the runtime's re-firing, not a loop to emit.

**No INTERFACE pragma.** The top owns the ports; the body only sees `hls::stream<ap_uint<W>>`
references wired to boundary ports or internal FIFOs.

**State leads the body.** A task has no "before the loop", so
[`HwState`](../memory/hwstate.md) declarations are emitted at the top of the body — the only place
persistent storage can live. That the value really survives re-firings is verified in RTL, not
assumed: `examples/state_toy`'s XSI gate fails loudly if it does not.

Alongside the body, `task_files_to_str` writes a `// TODO` stub for each
[`@synthesizable`](../custom_hooks/) hook. **Hook bodies are hand-written and are not lowered from the
Python** — the generator produces the body's *structure*, and nothing checks a hook against the Python
it mirrors.

## Ownership: what a task should own

The tasks in a network are not interchangeable, and the split is **structural, not stylistic**. A task
that owns an `m_axi` master should touch streams and tokens only, and should not also hold
stream-of-blocks locks; the block work belongs in its own task. The interleaver follows exactly this
split — `il_mem_r` / `il_mem_w` own the `m_axi`, while `il_load` / `il_compute` / `il_store` own the
lock-scoped block work.

Internal block channels lower to `hls::stream_of_blocks<T[N], depth>` (`SobEdge`), with
`hls::write_lock` / `hls::read_lock` **scopes inside the task bodies** — construction acquires, scope
exit commits. There are no lock *methods*; the braces are the handshake, and the producer's scope must
close before the consumer can acquire. Two throughput notes worth carrying into a design: a gather
(random reads) has the faster access path, while a scatter (random writes) serializes on write
hazards, so the two are not symmetric.

**Multi-master arbitration is deliberately not implemented.** The composite generator uses fixed
ownership boundaries (one read owner, one write owner) and explicit stage wiring rather than an
arbitration policy. A design needing two masters on one resource has to say so structurally.

## What is out of scope

A body that owns an `m_axi` master is **refused** by task-body emission (`_reject_m_axi_task`). That is
a scope boundary, not a law of HLS — a free-running task may carry `m_axi`, and the framework's own
`mem_r_stream_task.h` does. The emitter simply has not answered what an `m_axi` body needs (bundle
naming, depth, who owns the `offset=slave` register), so it refuses rather than emit something
unreviewed. Those bodies stay hand-written and are handed over via `kernel_task()`.

## See also

- [Concurrent (free-running)](../flows/concurrent.md) — the flow and when to choose it.
- [XSI testbench](./xsi_tb.md) — how the generated top is verified at RTL.
- [`HwState`](../memory/hwstate.md) — the storage those statics come from.
- [Extractor](./extractor.md) — what a `run_iter` body may contain.
