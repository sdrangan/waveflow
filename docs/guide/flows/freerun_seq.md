---
title: Free-running, sequentially driven
parent: Realization Flows
nav_order: 2
nav_exclude: true   # SUPERSEDED by flows/concurrent/ — kept for content mining; remove when harvested
audience: python
summary: "Flow 2 — a free-running DUT (a composite kernel: one hls::task for a leaf, one per child for a composite) verified at RTL by an XSI BFM, a cycle-based harness driving the design in xsim through XSI. Built: bit-exact for mem_r_stream / mem_copy / the interleaver, on a generated harness. This is now the single free-running flow — the former concurrent-SystemC Flow 3 was refuted and merged in."
---

# Flow 2 — Free-running kernel

> **This is now the single free-running flow, and it is built.** Two earlier drafts folded in here:
> the *concurrent SystemC* flow (old Flow 3) was refuted — the XSI BFM already drives every port
> cycle-by-cycle, so it is the concurrent harness (see [freerun_conc.md](./freerun_conc.md)); and the
> leaf-vs-composite target split (`free_running_kernel` / `composite_kernel`) collapsed to one target,
> `composite_kernel`, with the `FreeRunComp` merge (`plans/one_component_two_flows.md`). The BFM was
> hand-written when this page was first drafted; it is now **generated** (`tb_top_spec` +
> `render_tb_harness`) and bit-exact under the XSI gates.

**DUT output:** a **composite kernel** — one or more `ap_ctrl_none`
[tasks](../components/freerun.md) (`hls::task t(foo, …)`), either a single task compiled as its own top
(the 1-task case) or a [composite](../components/composite.md) network of them.
**Testbench:** an **XSI BFM** — a cycle-based harness that pumps the clock and models each
AXI-Stream / AXI-MM port, driving the elaborated RTL in `xsim` through **XSI**.

Why RTL and not co-sim: an `ap_ctrl_none` + `m_axi` block **cannot be Vitis co-simulated**, so the
moment the DUT is free-running the flow drops one level to XSI. One BFM covers both a single free-running
leaf (e.g. `mem_r_stream`, [`mem_r_bfm_tb.cpp`](../../../examples/interleaver/xsi/mem_r_bfm_tb.cpp)) and
a whole composite (e.g. the interleaver,
[`interleaver_canon_bfm_tb.cpp`](../../../examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp)).

> **Status: in work.** The RTL is generated and the flow *runs* bit-exact — but the BFM is
> **hand-written** today. Generating it from a `SeqTB` (a second `(class × target)` lowering of `main()`
> to an XSI-BFM `int main()`, plus a per-protocol BFM template library and an XSI build step) is the open
> problem. The interleaver — which already has both the generated RTL and a passing hand-written BFM to
> validate against — is the intended first complete example.
