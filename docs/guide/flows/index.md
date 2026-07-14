---
title: Realization Flows
parent: Guide
nav_order: 9.5
has_children: true
audience: python
summary: "The end-to-end recipes for taking a HwComponent from Python to a verified realization — which build steps run, in what order, producing which artifacts, and how the result is checked. Flows split along two axes: whether the DUT is control-driven (an ap_ctrl_hs kernel Vitis can co-simulate) or free-running (an ap_ctrl_none kernel or composite it cannot), and whether a sequential or concurrent testbench drives it. Four flows: control-driven kernel + sequential Vitis TB (built), free-running + sequential XSI BFM (in work), free-running + concurrent SystemC (in work), and the full system on hardware."
---

# Realization Flows

[Hardware Components](../components/) tells you *how to write* a component; the [Build System](../build/)
is the *machinery* — the `BuildDag` of typed steps. This section is the **connective tissue**: the
end-to-end **recipe** for taking a component from Python to a *verified realization* — which steps run,
in what order, producing which artifacts, and how the result is checked — one recipe per realization
path.

The paths split along two axes:

- **The DUT** — is it **control-driven** (an `ap_ctrl_hs` kernel the host launches, which Vitis *can*
  co-simulate) or **free-running** (an `ap_ctrl_none` kernel or composite, which it *cannot* — so it
  drops to RTL)?
- **The testbench** — does a **sequential** program drive it, or a **concurrent** one?

| Flow | DUT output | Testbench | Status |
|---|---|---|---|
| [1 · Control-driven kernel](./host_seqtb_cosim.md) | a **control-driven kernel** (`ap_ctrl_hs` + `s_axilite`) | **sequential Vitis TB** — a [`SeqTB`](../components/) run in C-sim / co-sim | **Built** |
| [2 · Free-running, sequentially driven](./freerun_seq.md) | a **free-running kernel** *or* **composite kernel** (`ap_ctrl_none`) | **sequential XSI TB** — a cycle-based BFM | **In work** — BFM hand-written |
| [3 · Free-running, concurrently driven](./freerun_conc.md) | *same* free-running / composite kernel | **concurrent SystemC TB** — `SC_THREAD` agents | **In work** |
| [4 · Full system, on hardware](./bitstream_ipi.md) | an **FPGA bitstream** (IPI system) | host software (no TB) | **Future** |

A free-running DUT is built from **tasks** — a free-running function `foo` instantiated as
`hls::task t(foo, …)`. One task compiled as its own top is a **free-running kernel**; a network of them
wired by internal channels is a **composite kernel**. Both are `ap_ctrl_none`, and both are driven the
same way — so Flow 2 covers a single free-running leaf (e.g. `mem_r_stream`) *and* a composite (e.g. the
interleaver) with one BFM.

Read it as a story: flows **1 → 2** keep a *sequential* testbench but move from a co-simulable
control-driven kernel to a free-running DUT that must be driven at RTL; **2 → 3** keep the free-running
DUT but swap the sequential BFM for a *concurrent* SystemC harness (the move that lifts the
sequential-testbench limitation); **4** leaves simulation entirely for the fabric.

## See also

- [Hardware Components](../components/) — *what* you are realizing (the component kinds these flows take as input).
- [Build System](../build/) — the `BuildDag` machinery these recipes invoke.
- [Build targets](../../overview/targets.md) — the realization *matrix* (roles × targets) this section walks cell by cell.
