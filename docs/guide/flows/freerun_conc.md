---
title: Free-running, concurrently driven
parent: Realization Flows
nav_order: 3
nav_exclude: true   # SUPERSEDED (Flow 3 refuted, merged into flows/concurrent/) — kept for the record
audience: python
summary: "Flow 3 — the same free-running DUT as Flow 2 (a free-running kernel or composite of tasks), driven by a concurrent SystemC TB (one SC_THREAD per channel) in xsim through XSI, instead of a single sequential BFM. The long-term flow: it restores the parallel-process model at the RTL level. The XSI+SystemC mechanism is proven in isolation; generating the harness is future work."
---

# Flow 3 — Free-running, concurrently driven

> ⚠️ **SUPERSEDED — this flow was refuted and merged into [Flow 2](./freerun_seq.md).** The premise
> here was that lifting Vitis's sequential-testbench limitation needed a concurrent SystemC harness
> (one `SC_THREAD` per agent). It does not: the XSI BFM in Flow 2 drives every port cycle-by-cycle, so
> it already *is* the concurrent harness — there is no sequential/concurrent split to justify two flows.
> The realization vocabulary no longer contains `concurrent_systemc_tb`, and the flow table lists three
> flows, not four. See `plans/xsi_tb_codegen.md` (SystemC refuted) and `plans/one_component_two_flows.md`
> (the leaf/composite merge). This page is kept only for the design-history record below; nothing
> generates a SystemC harness.

**DUT output:** the *same* **free-running kernel** or **composite kernel** as the
[sequentially driven](./freerun_seq.md) flow — one `ap_ctrl_none` [task](components.md) or a
[composite](components.md) network of them.
**Testbench:** a **concurrent SystemC TB** — a harness where each stimulus/capture agent is its own
`SC_THREAD`, driving the elaborated RTL in `xsim` through **XSI**.

The DUT is identical to the [sequentially driven](./freerun_seq.md) flow; only the driver changes. Making it concurrent lifts the
single-threaded-BFM limitation — independent streams get independent, concurrent drivers with real
backpressure, restoring the simulation model's parallel-process feel at the RTL level.

> **Status: in work (long-term).** The mechanism — a SystemC / C++ testbench driving RTL in `xsim` via
> XSI — is **proven in isolation** (the `xelab -dll` → `xsimk.dll` + `xsi_loader` path). What is missing
> is *generating* the concurrent harness from the Python design. Detailed walkthrough deferred until the
> code lands.
