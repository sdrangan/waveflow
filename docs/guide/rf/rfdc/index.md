---
title: Rfdc
parent: RF converters
nav_order: 1
has_children: true
audience: python
summary: "The converter itself: what Rfdc models, how to instantiate and wire it, its two sides — a block-rate sample channel on the RF domain and an ordinary AXI-Stream on the fabric — the block-sampling model underneath, and what that model cannot tell you."
---

# Rfdc

Waveflow's RF converter is a basic model for a DAC and ADC converter interfacing to logic via an
AXI stream.  The model follows
[AMD's RF Data Converter IP](https://www.amd.com/en/products/adaptive-socs-and-fpgas/intellectual-property/rf-data-converter.html).
The AMD RFDC also provides **configurable** digital up- and down-conversion — NCO frequency, mixer
settings, interpolation and decimation factors. Waveflow does not model that configuration capability
today; over time, we will emulate it. Carrying I/Q samples is a separate question and *is* supported —
see [real and I/Q](./iqmode.md).

## Interfaces

In Waveflow, the python class for the converter is `Rfdc` and represents  **one module carrying both directions**, with a different kind of port on each side:

![Rfdc between your logic and the RF environment: rx_rf and tx_rf carry sample blocks on the RF side, rx_streams[0] and tx_streams[0] carry packed words on the fabric side](../figures/rf_interfaces.svg)

| side | endpoints | carries | clock |
|---|---|---|---|
| **RF** | `rx_rf`, `tx_rf` — `RFSampIF` | **blocks** of samples | `samp_clk`, up to GSa/s |
| **fabric** | `rx_streams[i]`, `tx_streams[i]` — `StreamIF`, **one per channel** | **words** on AXI-Stream | `axis_clk`, a few hundred MHz |

Two facts fall out of that picture, and they set the order of everything below.

**The two sides run at rates an order of magnitude apart.** A converter samples far faster than the
logic behind it, so it cannot hand over one sample per fabric cycle. The AXI-Stream carries several
samples **packed into each beat** — which means that before you can instantiate an `Rfdc` at all,
you have to say how many, and how they are arranged. That is what the first page is for.

**Only the stream endpoints cross the [cut](../../flows/modules.md#the-cut).** The RF endpoints have no
RTL counterpart — they are the boundary whose far side we refuse to model at RTL. That asymmetry is
why the two sides read so differently in the pages that follow.

## Pages

The order is **do → understand → limits**:

- [Multi-channel support](./channels.md) — how many datapaths a converter has, and what a channel
  costs on each of its two sides.
- [Real and I/Q](./iqmode.md) — what a *sample* is, and the two flags that say so.
- [Adding an RF path](./quickstart.md) — wire one up and run it; the shortest path to samples flowing.
- [The sample word](./word.md) — why converters pack at all, and which sample lands where.
- [Instantiating the converter](./converter.md) — the parameter split, the ports, what it refuses.
- [Connecting the RF side](./rf_side.md) — `RFSampIF`, sources and sinks, `t0`.
- [Connecting the fabric side](./axis_side.md) — the AXI-Stream wiring, the rate check, and the loss
  counters.
- [Block sampling](./sampling.md) — the model you have been using: the block, the metronome, the grid.
- [The design rules](./rules.md) — seven things that make a design wrong if you break them.
- [The fidelity boundary](./fidelity.md) — what this modelling can and cannot tell you.

**The design rules are late, but read them early if you are already building.** They are assembled
from the pages before them, so they read best once you have seen where each came from — but rules 1–4
make a design correct and 5–7 make it checkable, and a rule broken at design time costs more than one
read out of order.
