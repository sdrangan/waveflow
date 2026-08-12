---
title: Concurrent (free-running)
parent: Hardware modules and Flows
nav_order: 3
has_children: true
audience: python
summary: "Flow 2 — a free-running (ap_ctrl_none) kernel or composite Vitis cannot co-simulate, verified by driving the elaborated RTL cycle-by-cycle through a concurrent XSI BFM. The concept here; the full worked walkthrough is the mem_copy example."
---

# Concurrent (free-running) flow

The concurrent flow is for a DUT that **runs on its own**. The kernel is `ap_ctrl_none`: no start/done
handshake — one `hls::task` for a leaf, one per child for a composite, wired by internal channels. It
never waits to be launched and never returns; a leaf simply *re-fires* on each new job. In Python it is
a [`FreeRunMod`](./modules.md) — a leaf implements `run_iter`, a composite has sub-components.

Because it never returns, there is nothing for a sequential program to *call* — which changes both what
it can do and how it must be verified.

## Why free-running

A free-running design **overlaps work**. Jobs pipeline across it: the reads of job *j+1* can proceed
while job *j* is still being stored, and in a composite each stage runs concurrently with the others.
That is throughput the [sequential flow](./sequential.md) cannot get, because there the host serializes
one call at a time.

The cost is verification. Vitis C/RTL co-simulation drives a kernel through its `ap_start`/`ap_done`
handshake — and a free-running kernel has none, so **co-sim refuses it**. Verification instead drives
the elaborated RTL directly, cycle by cycle, through an **XSI BFM** (a cycle-based bus-functional
model). The BFM drives every port each cycle, so it *is* a concurrent harness — which is why there is
no separate SystemC flow. An earlier draft had one; it was refuted, because the XSI BFM already does
what a SystemC testbench would have been for.

The specific blocker is worth naming: an `ap_ctrl_none` block that also carries `m_axi` cannot be
Vitis co-simulated at all. The moment a DUT is free-running, verification drops a level to XSI.

## When to use it, and what it costs

**Use it when** the design is a dataflow/streaming accelerator, or any multi-task composite whose
stages should run concurrently — anything that benefits from pipelining rather than one-shot invocation.

- **+** True concurrency and cross-job pipelining — the throughput a host-serialized kernel cannot get.
- **+** Composites: a network of `hls::task` stages, each running independently.
- **−** No Vitis co-sim — verification is at RTL through a hand-built (but *generated*) XSI harness.
- **−** More machinery: internal channels, per-job tokens, and the RTL/XSI toolchain (xsim).

## The DUT / TB boundary {#dut-tb-boundary}

This flow has a **cut**: some modules are synthesized into the top, the rest become models beside it.
It is worth making explicit, because none of it is declared anywhere.

**The boundary is derived.** A composite names only its external port *names* — and only because
local names collide (both `MemRStream` and `MemWStream` call their memory port `m_mem`, and the top
needs `m_in` / `m_out`). Everything else is read off the graph: *a child endpoint not bound to one of
the composite's internal interfaces **is** a boundary port*, in `add_comp` × `add_endpoint` order.
The direction comes from the endpoint's type, and the `gmem` bundles from the assembler's policy.

**The cut is a build choice.** `tb_top_spec(tb, dut=...)` names which child is synthesized;
everything else in the graph becomes a testbench model. Discovery — "the one child with a
`boundary`" — is the default, not the mechanism. So which modules are inside the boundary is a
property of *this build*, not of the classes. See
[Hardware modules](./modules.md#the-cut).

**An internal channel and a boundary port are different objects.** This is the part that surprises
people, and it is a Vitis constraint rather than a design preference:

| | internal channel | boundary port |
|---|---|---|
| carries a packet boundary as | `streamutils::framed_word<W>` (a plain struct) | a real `TLAST` wire |
| C++ type | `hls::stream<framed_word<W>>` | `hls::stream<ap_axis<W,0,0,0>>` |
| why | HLS 214-208: `ap_axis` is reserved for interface ports and rejected on an internal FIFO | it *is* an interface port |

`framed_word`'s members are deliberately named to match `ap_axis` (`.data` / `.last`) so one
templated helper — `read_boundary_word<WordT, W>` — serves both.

### Moving the cut: what it costs today

The capability is real in the **graph** and not yet real in the **artifact**, and it is worth being
precise about which is which.

`MemRStream` genuinely is generated at two cuts: as its own top
([`examples/interleaver/gen/mem_r_stream.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/gen/mem_r_stream.cpp),
XSI gate **158**) and as a task inside `mem_copy`
([`examples/mem_copy/gen/mem_copy.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/gen/mem_copy.cpp),
gate **2908**). But those two are **two protocols**, not one module at two cuts: the standalone one
reads an `MRCmd` and bursts; the composite one reads a `MemRCmd` and relays `fwd_bursts` opaque
bursts first. `inband` is a `HwParam` — a build-time parameter of the *design* — precisely because it
selects a protocol, and the framing follows from the protocol rather than the other way round.

Holding the protocol fixed and moving *only* the cut does not work yet. Ask the generator for the
in-band reader as a standalone top and it will emit this:

```cpp
void mem_r_stream(hls::stream<ap_uint<64> >& s_cmd, ...) {          // plain words at the boundary
    hls_thread_local hls::task t0(mem_r_stream_framed_task<64>, s_cmd, m_mem, m_out);
}                                          // ...but the body's signature demands framed_word<64>
```

That does not compile, and nothing in Python catches it. The task body's argument word types are not
part of `kernel_task()`'s contract, so the generator cannot check them — and the obvious proxy does
not work either: `mem_copy`'s own `s_done` endpoint is `has_tlast=True` in Python while
`mem_w_stream_framed_done_task` declares it a plain `ap_uint` stream, and that design is the 2908
gate. The Python framing flag and the C++ word type already disagree on a *working* design.

Making the cut free in the artifact means teaching `kernel_task()` about the cut. That is designed
but not built — see `plans/design_cut.md` §S5.

## How to read this flow

- **[Writing it in Python](./concurrent_python.md)** — how to describe the module: a leaf's
  `run_iter` (one firing) versus a composite's graph, and carrying state across firings.
- The **[flow steps](./concurrent_flowsteps.md)** page is the recipe — from the component graph to a
  generated top, generated XSI harness, and an exact cycle-count check.
- The **[mem_copy example](../../examples/memcpy/)** is the full worked walkthrough: the composite
  (`Sequencer → MemRStream → MemWStream`), the generated `ap_ctrl_none` top, the generated XSI
  testbench, and the RTL/XSI verification.
- The framework memory streamers it is built from are the
  [Streaming Memory Kernels](../memory/memstream.md) page.
- **[How it is realized in HLS](../comp_codegen/freerunning.md)** — the generated `ap_ctrl_none` top,
  the task bodies, and [the XSI testbench](../comp_codegen/xsi_tb.md).

The XSI gates are **exact cycle counts**, not bounds: a count that moves is either a real regression
or a real improvement, and both deserve a human look.

**Targets:** `composite_kernel` (the DUT — one target for a leaf and a composite alike, a leaf being
the 1-task case) + `sequential_xsi_tb` (the XSI testbench) — both built.
