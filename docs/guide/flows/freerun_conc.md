---
title: Free-running, concurrently driven
parent: Realization Flows
nav_order: 3
audience: python
summary: "Flow 3 — the same free-running DUT as Flow 2 (a free-running kernel or composite of tasks), driven by a concurrent SystemC TB (one SC_THREAD per channel) in xsim through XSI, instead of a single sequential BFM. The long-term flow: it restores the parallel-process model at the RTL level. The XSI+SystemC mechanism is proven in isolation; generating the harness is future work."
---

# Flow 3 — Free-running, concurrently driven

**DUT output:** the *same* **free-running kernel** or **composite kernel** as the
[sequentially driven](./freerun_seq.md) flow — one `ap_ctrl_none` [task](../components/freerun.md) or a
[composite](../components/composite.md) network of them.
**Testbench:** a **concurrent SystemC TB** — a harness where each stimulus/capture agent is its own
`SC_THREAD`, driving the elaborated RTL in `xsim` through **XSI**.

The DUT is identical to the [sequentially driven](./freerun_seq.md) flow; only the driver changes. Making it concurrent lifts the
single-threaded-BFM limitation — independent streams get independent, concurrent drivers with real
backpressure, restoring the simulation model's parallel-process feel at the RTL level.

> **Status: in work (long-term).** The mechanism — a SystemC / C++ testbench driving RTL in `xsim` via
> XSI — is **proven in isolation** (the `xelab -dll` → `xsimk.dll` + `xsi_loader` path). What is missing
> is *generating* the concurrent harness from the Python design. Detailed walkthrough deferred until the
> code lands.
