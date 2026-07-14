---
title: Free-running, sequentially driven
parent: Realization Flows
nav_order: 2
audience: python
summary: "Flow 2 — a free-running DUT (a single free-running kernel or a composite kernel of hls::task tiles) verified at RTL by a sequential XSI TB: a cycle-based BFM driving the design in xsim through XSI. In work: the BFM is hand-written today; generating it from a SeqTB is the open build-step problem, with the interleaver as the intended first complete example."
---

# Flow 2 — Free-running, sequentially driven

**DUT output:** a **free-running kernel** or a **composite kernel** — one or more `ap_ctrl_none`
[tasks](../components/freerun.md) (`hls::task t(foo, …)`), either a single task compiled as its own top
or a [composite](../components/composite.md) network of them.
**Testbench:** a **sequential XSI TB** — a cycle-based **BFM** that pumps the clock and models each
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
