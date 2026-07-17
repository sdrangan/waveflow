---
title: Realization Flows
parent: Guide
nav_order: 9.5
has_children: true
audience: python
summary: "The end-to-end recipes for taking a HwComponent from Python to a verified realization — which build steps run, in what order, producing which artifacts, and how the result is checked. Flows split on the DUT: control-driven (an ap_ctrl_hs kernel Vitis can co-simulate) or free-running (an ap_ctrl_none kernel or composite it cannot, so it drops to RTL under an XSI BFM). Three flows: control-driven kernel + sequential Vitis TB (built), free-running kernel + concurrent XSI BFM (built), and the full system on hardware (future)."
---

# Realization Flows

[Hardware Components](../components/) tells you *how to write* a component; the [Build System](../build/)
is the *machinery* — the `BuildDag` of typed steps. This section is the **connective tissue**: the
end-to-end **recipe** for taking a component from Python to a *verified realization* — which steps run,
in what order, producing which artifacts, and how the result is checked — one recipe per realization
path.

The paths split on **the DUT**: is it **control-driven** (an `ap_ctrl_hs` kernel the host launches,
which Vitis *can* co-simulate) or **free-running** (an `ap_ctrl_none` kernel or composite, which it
*cannot* — so it drops to RTL under a cycle-based XSI BFM)?

| Flow | DUT output | Testbench | Status |
|---|---|---|---|
| [1 · Control-driven kernel](./control_kernel.md) | a **control-driven kernel** (`ap_ctrl_hs` + `s_axilite`) | **sequential Vitis TB** — a [`SeqTB`](../components/) run in C-sim / co-sim | **Built** |
| [2 · Free-running kernel](./freerun_seq.md) | a **composite kernel** (`ap_ctrl_none`) — one `hls::task` for a leaf, one per child for a composite | **XSI BFM** — a cycle-based harness driving the RTL | **Built** |
| [3 · Full system, on hardware](./bitstream_ipi.md) | an **FPGA bitstream** (IPI system) | host software (no TB) | **Future** |

A free-running DUT is built from **tasks** — a free-running function `foo` instantiated as
`hls::task t(foo, …)`. There is **one DUT target, `composite_kernel`**: a single task compiled as its
own top is just the 1-task case of a network of tasks wired by internal channels, so the same generator
([`composite_top_spec`](../../../waveflow/build/composite_gen.py)) emits a free-running leaf (e.g.
`mem_r_stream`) *and* a composite (e.g. the interleaver), driven by one BFM.

> **Two former flows collapsed into this one.** Earlier drafts split the free-running path in two — a
> *sequential* XSI BFM (old Flow 2) and a *concurrent* SystemC harness (old Flow 3) — on the theory that
> lifting Vitis's sequential-testbench limitation needed SystemC. It did not: the XSI BFM drives every
> port cycle-by-cycle, so it *is* the concurrent harness, and the SystemC flow was refuted (see
> `plans/xsi_tb_codegen.md`). And the leaf/composite split that made `free_running_kernel` and
> `composite_kernel` two targets was removed with the `FreeRunComp` merge (`plans/one_component_two_flows.md`),
> leaving one. So the four-flow, two-axis picture became three flows on one axis.

Read it as a story: flows **1 → 2** move from a co-simulable control-driven kernel to a free-running DUT
that must be driven at RTL by the BFM; **3** leaves simulation entirely for the fabric.

## See also

- [Hardware Components](../components/) — *what* you are realizing (the component kinds these flows take as input).
- [Build System](../build/) — the `BuildDag` machinery these recipes invoke.
- [Build targets](../../overview/targets.md) — the realization *matrix* (roles × targets) this section walks cell by cell.
