---
title: Rfdc
parent: RF converters
nav_order: 1
has_children: true
audience: python
summary: "The converter itself: what Rfdc models, how to instantiate and wire it, its two sides — a block-rate sample channel on the RF domain and an ordinary AXI-Stream on the fabric — the block-sampling model underneath, and what that model cannot tell you."
---

# RF converters — the Python model

Every RF design starts here. Nothing on these pages needs a toolchain.

The order is **do → understand → limits**, which is not the order any of it was discovered in:

| | page | what it is for |
|---|---|---|
| 1 | [Quickstart](./quickstart.md) | wire one up and run it — the shortest path to samples flowing |
| 2 | [Instantiating the converter](./converter.md) | the parameter split, the four ports, what it refuses |
| 3 | [Connecting the RF side](./rf_side.md) | `RFSampIF`, sources and sinks, `t0` |
| 4 | [Connecting the fabric side](./axis_side.md) | packing, `samp_per_word`, and the rate check |
| 5 | [Block sampling](./sampling.md) | the model you have been using: the block, the metronome, the grid |
| 7 | [The design rules](./rules.md) | seven things that make a design wrong if you break them |
| 8 | [The fidelity boundary](./fidelity.md) | what this modelling can and cannot tell you |

Two placement decisions worth their reasons.

**The sampling model comes *after* the wiring pages.** A reader adding a first converter does not
need block-LT theory to get output on a sink; they need to know `blksize` is a knob, which the
quickstart says in a line. The capture buffer genuinely does need the sample grid — its four command
cases are all in sample indices — so the model sits between them.

**`rules.md` is late but you may read it early.** It is assembled from the four pages before it, so
it reads best once you have seen where each rule came from. If you are already building, read it
first anyway: 1–4 make a design correct and 5–7 make it checkable, and a rule broken at design time
costs more than one read out of order.
