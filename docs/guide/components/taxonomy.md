---
title: Component taxonomy
parent: Hardware Components
nav_order: 2
audience: python
applies_to: [HwComponent]
api: [HwComponent, VitisRegMapMMIFSlave, synthesizable, HwTestbench]
summary: "The kinds of HwComponent, classified by role: synthesizable (host-activated on_start vs. free-running run_proc, the latter ap_ctrl_hs or ap_ctrl_none), testbench (sequential now, SystemC later), and behavioral simulation-only models of hardware Waveflow does not generate (RFDC, memories, channels). A component's role is fixed; its realization changes with the build target."
---

# Component taxonomy

[Defining a component](./overview.md) walked through *one* component — a synthesizable,
regmap-launched kernel. But a `HwComponent` is more general than that: **not every component is
synthesizable, and the synthesizable ones come in several kinds.** This page classifies them.

Classify by **role** — the function a component serves — because that, not its Python shape, is what
determines how it is realized. And a component's realization *changes with the target*: behavioral in
simulation, real in a bitstream (the [Build targets](../../overview/targets.md) view is by column — by
target). Here we go by row — by role.

```
HwComponent
├── Synthesizable  (SynthComp)      @synthesizable compute → codegen emits a Vitis kernel
│   ├── Host-activated              on_start · ap_ctrl_hs · runs once per trigger (regmap-carrying)
│   └── Free-running  (FreeRunComp) run_iter · one firing per job, looped by the base
│       ├── ap_ctrl_none    the hls::task tiles of a composite (XSI-only — can't Vitis-cosim)
│       └── ap_ctrl_hs      a single self-looping kernel (LoopComp, future — cosim-able)
├── Testbench  (HwTestbench)        main() → a driver, not logic
│   ├── Sequential         the sequential C++ testbench (today)
│   └── SystemC/SC_THREAD   concurrent TB for free-running & multi-block designs (future)
└── Behavioral                     plain HwComponent · no @synthesizable · never run through codegen
    (vendor IP: RFDC ADC/DAC · memories · RF channels · host/DMA models)
```

## Synthesizable

A component with [`@synthesizable`](../../../waveflow/hw/synth.py) compute; codegen turns it into a
Vitis HLS kernel (its C++ realization is [Component structure](../comp_codegen/structure.md)).
Synthesizable components subclass [`SynthComp`](../../../waveflow/hw/hw_component.py) — the
"generates C++" base, whose `__post_init__` runs a construction-time **synthesizability check**. Which
*kind* of synthesizable component it is follows from the concrete class you pick; each class declares
its `control_mode` and its kernel-entry method.

### Host-activated (invocation)

Carries a [`VitisRegMapMMIFSlave`](./endpoints.md); you implement **`on_start`**. The host writes
`ap_start`; the kernel reads its inputs from the register map, computes, writes the results, and returns
— one run per trigger (`ap_ctrl_hs`, with the `ap_start` / `ap_done` handshake). `simp_fun` is the
minimal case; `poly` uses this as a control path alongside streamed sample data.

### Free-running (continuous)

Subclass [`FreeRunComp`](../../../waveflow/hw/hw_freerun.py) and implement **`run_iter`** — *one firing*
(the body the `hls::task` runtime re-fires; the infinite loop is the base's, not yours). `FreeRunComp`
sets `control_mode = FREE_RUNNING` explicitly, so codegen never has to detect a `while` loop at the
root. The HLS control protocol splits this further, and the split matters for verification:

- **`ap_ctrl_none`** — the truly free-running `hls::task` tiles of a composite. They carry no control
  ports, and an `ap_ctrl_none` + `m_axi` kernel **cannot be Vitis-cosim'd**, so it is verified on the
  [XSI rung](../build/xsi.md) instead. The [interleaver](../concurrency/) tiles are this kind.
- **`ap_ctrl_hs`** — a single self-looping kernel (started once, runs until done). Cosim-able; a
  dedicated `LoopComp` for it is future work.

## Testbench

`is_testbench=True` (an [`HwTestbench`](./hwtestbench.md) subclass); codegen routes to **`main()`** and
emits a *driver* rather than synthesized logic — its job is to *exercise* synthesizable kernels
([Testbench codegen](../comp_codegen/testbench.md)).

- **Sequential** (today) — the sequential C++ testbench that drives C-sim and co-sim.
- **SystemC / `SC_THREAD`** (future) — a concurrent testbench for free-running and multi-block designs,
  run in xsim. Proven in isolation, not yet integrated.

## Behavioral (simulation-only)

A component with **no `@synthesizable` methods that you never run through codegen** — a pure
[SimPy](../sim/) model of hardware Waveflow does not generate. This is the category the "synthesizable
module" definition misses, and it is first-class: data converters (the RFSoC **RFDC** ADC/DAC), external
memories, RF channels, and host / DMA models all live here. They are *behavioral* at every simulation
target and become **real vendor IP** only in the bitstream — the
[realization duality](../../overview/targets.md#not-every-block-realizes-the-same-way). (They're
identified by role and usage, not by a flag — you simply never point the generator at them.)

> **Interconnect is not a component.** AXI interconnect, the memory controller, and DMA plumbing aren't
> `HwComponent`s at all in simulation — they are the [`Interface`](../interface/) *wiring* between
> components, implicit until the bitstream, where the (future) IPI flow emits them as generated IP.
> They appear on the [target matrix](../../overview/targets.md) but off this role tree.

## How the kind is selected

The kind is the component's **class**, which declares its kernel-entry method and `control_mode` (the
selection lives in
[`extract_kernel`](../comp_codegen/structure.md#the-execution-model-free-running-vs-regmap-launched)):

| The component's class… | Role | Kernel entry |
|---|---|---|
| [`HwTestbench`](./hwtestbench.md) (`is_testbench`) | Testbench | `main()` |
| [`FreeRunComp`](../../../waveflow/hw/hw_freerun.py) | Free-running (`ap_ctrl_none`) | `run_iter` |
| carries a `VitisRegMapMMIFSlave` | Host-activated | `on_start` |
| plain `HwComponent`, no `@synthesizable` | Behavioral | — (simulation only) |

`FreeRunComp` and `SynthComp` exist today — they declare the execution model and check synthesizability
at construction. Explicit `run_iter` *extraction* in `extract_kernel` lands with the first
auto-extracted free-running kernel; the current free-running components use fixed template bodies.

## See also

- [Defining a component](./overview.md) — the `HwComponent` class and how you declare its endpoints.
- [Build targets](../../overview/targets.md) — the *by-target* view: how each role realizes down the fidelity ladder.
- [Component structure](../comp_codegen/structure.md) — the C++ realization of the synthesizable roles and the selection code.
- [Testbench](../comp_codegen/testbench.md) — the `is_testbench` codegen mode.
- [Concurrency](../concurrency/) — free-running `ap_ctrl_none` composites (the interleaver).
