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
├── Synthesizable        @synthesizable compute → codegen emits a Vitis kernel
│   ├── Host-activated    carries a VitisRegMapMMIFSlave · you write on_start · runs once per trigger
│   └── Free-running      no regmap · you write run_proc · loops over its ports forever
│       ├── ap_ctrl_hs      a single self-looping kernel (cosim-able)
│       └── ap_ctrl_none    the hls::task tiles of a composite (XSI-only — can't Vitis-cosim)
├── Testbench            is_testbench / HwTestbench → codegen routes to main() (a driver, not logic)
│   ├── Sequential         the sequential C++ testbench (today)
│   └── SystemC/SC_THREAD   concurrent TB for free-running & multi-block designs (future)
└── Behavioral           no @synthesizable · never run through codegen · a SimPy model only
    (vendor IP: RFDC ADC/DAC · memories · RF channels · host/DMA models)
```

## Synthesizable

A component with [`@synthesizable`](../../../waveflow/hw/synth.py) compute; codegen turns it into a
Vitis HLS kernel (its C++ realization is [Component structure](../comp_codegen/structure.md)). As
[overview noted](./overview.md#execution-models), the kind is selected **automatically** from the
component's endpoints — you don't set it.

### Host-activated (invocation)

Carries a [`VitisRegMapMMIFSlave`](./endpoints.md); you implement **`on_start`**. The host writes
`ap_start`; the kernel reads its inputs from the register map, computes, writes the results, and returns
— one run per trigger (`ap_ctrl_hs`, with the `ap_start` / `ap_done` handshake). `simp_fun` is the
minimal case; `poly` uses this as a control path alongside streamed sample data.

### Free-running (continuous)

No regmap; you implement **`run_proc`**, a long-lived loop over the component's stream / `m_axi` ports.
The HLS control protocol splits this further, and the split matters for verification:

- **`ap_ctrl_hs`** — a single self-looping kernel (started once, runs until done). Cosim-able.
- **`ap_ctrl_none`** — the truly free-running `hls::task` tiles of a composite. They carry no control
  ports, and an `ap_ctrl_none` + `m_axi` kernel **cannot be Vitis-cosim'd**, so it is verified on the
  [XSI rung](../build/xsi.md) instead. The [interleaver](../concurrency/) tiles are this kind.

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

The generator picks the mode from the component, in this order (the selection code lives in
[`extract_kernel`](../comp_codegen/structure.md#the-execution-model-free-running-vs-regmap-launched)):

| The component… | Role | Kernel entry |
|---|---|---|
| is an `HwTestbench` / `is_testbench=True` | Testbench | `main()` |
| carries a `VitisRegMapMMIFSlave` | Host-activated | `on_start` |
| otherwise (and is run through codegen) | Free-running | `run_proc` |
| is never run through codegen (no `@synthesizable`) | Behavioral | — (simulation only) |

## See also

- [Defining a component](./overview.md) — the `HwComponent` class and how you declare its endpoints.
- [Build targets](../../overview/targets.md) — the *by-target* view: how each role realizes down the fidelity ladder.
- [Component structure](../comp_codegen/structure.md) — the C++ realization of the synthesizable roles and the selection code.
- [Testbench](../comp_codegen/testbench.md) — the `is_testbench` codegen mode.
- [Concurrency](../concurrency/) — free-running `ap_ctrl_none` composites (the interleaver).
