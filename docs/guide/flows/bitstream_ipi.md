---
title: Toward the board
parent: Hardware modules and Flows
nav_order: 4
audience: python
summary: "What follows either simulation flow when a system goes to a board: its parts packaged as Vivado IP, one per device, and wired in a block design from the same spec as the XSI system top. Packaging and synthesis mode are independent choices: a block design of device IPs can be synthesized globally (one run, best area, fastest cold build: the default for a bitstream) or per device (each part's number its own, an incremental change cheap: for design-space exploration). Built through a validated block design; the processor, the board and the bitstream come next."
---

# Toward the board

The two flows end in a **verified** realization: a kernel co-simulated by Vitis, or a system run at
RTL through XSI. Neither is on a board yet. This page is what follows either one when a system goes
to the fabric. It is not a third flow: there is no new testbench and no new verification rung, only
the same verified design put in the form a Vivado block design holds.

## The device is the unit

A system's DUT is already rendered as a tree of **devices** (the [XSI system
top](../build/xsi_system.md#devices)): each kernel with its memory-mapped views, its AXI front and
the bus writers that act for it, plus the on-chip memory. The board flow keeps that unit:

- **each device and the memory is packaged as an IP**, from exactly the files the XSI gates
  compile ([IP packaging](../build/ipi.md));
- **AMD's crossbar** is used as the IP it already is;
- **a block design** instantiates them and wires them from the same `SystemTopSpec` the XSI top is
  rendered from, then `validate_bd_design` checks it ([the block
  design](../build/ipi.md#the-block-design)).

Every link between devices is AXI4 or an interrupt line: the streams, credit pairs and FIFOs are
inside a device. So the block design needs no custom interface definitions, and each device is a
cell the board can drive on its own.

## Two independent choices

Two decisions are easy to confuse, because they are usually made together:

| choice | options |
|---|---|
| **packaging**: what is an IP | one per device (the default here), or the whole system as one |
| **synthesis mode**: how Vivado runs | **global** (one run over everything) or **per device** (each part out of context, then linked) |

A block design of device IPs can still be synthesized globally, and a generated top can be
synthesized per device ([per device](../resource/systems.md#per-device)). On markov the two
synthesis modes differ in time, not in area:

| markov, xc7z020, 10 ns | LUT | Vivado seconds, cold | after a one-device change (slowest) |
|---|---:|---:|---:|
| global + `opt_design` | 6,228 | 132 s | 132 s |
| per device, linked | 6,250 (+0.35%) | 226 s | 84 s |

(markov's [route comparison](../../examples/markov/synth.md#routes) has the parts, the link term and
the figure; the seconds are Vivado's own run timers, start-up excluded.)

## Choosing per purpose

| purpose | packaging | synthesis |
|---|---|---|
| final bitstream | per device | **global**: best area and timing, the fastest cold build |
| design-space exploration, incremental work | per device | per device: each configuration paid once |
| one device on the board alone | that device's IP | its own small block design |

**The default is global synthesis** (`system_synth`'s `synth_route="global"`, and the block design's
`synth_checkpoint_mode` set to `None`). Losing optimization across device boundaries costs only
0.35% LUT on markov, so either mode would do for area. Global wins on the measure that decides a
bitstream build, which is cold: one run is faster than several that each pay a run's fixed
overhead.
Per device pays off when one part changes and the rest does not, which is a design-space search's
loop, not a bitstream's.

## Status

Built: the packaging and a validated block design, both opt-in steps on every system's DAG and both
gated on markov (`--through block_design`). Next, from `plans/board_packaging.md`: the processor and
its interconnect in the block design, the board's part, and the bitstream run with host software
driving it.
