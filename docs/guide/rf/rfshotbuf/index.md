---
title: RfShotBuf
parent: RF converters
nav_order: 2
has_children: true
audience: python
summary: "The finite sample buffer: for designs where nothing reads the memory while something else is writing it. Two classes — RfShotTx plays a stored waveform, RfShotRx captures into a memory a reader drains behind it — both built on a lock over one true-dual-port BRAM, and both gated through real Verilog."
---

# RfShotBuf

`RfShotBuf` is the **finite** sample buffer — load a waveform, *then* play it; capture into a region,
*then* transfer it. Because a writer and a reader never touch the same words at the same time there
is nothing to arbitrate in the streaming sense: no credit, no acknowledgements, no progress pointer,
no staleness margin. All of the memory is payload.

> **`RfShotBuf` is a family name, not a class.** There is no `RfShotBuf` to import. The family is two
> concrete modules, and you reach for one of them by direction.

| | class | module | example | RTL gate |
|---|---|---|---|---|
| **transmit** — play a stored waveform | [`RfShotTx`](./tx.md) | `waveflow/hw/rf_shot_tx.py` | `examples/rf_shot_tx` | `tests/examples/test_rf_shot_tx_xsi.py` |
| **receive** — capture continuously | [`RfShotRx`](./rx.md) | `waveflow/hw/rf_shot_rx.py` | `examples/rf_shot_rx` | `tests/examples/test_rf_shot_rx_xsi.py` |

Both are **built and gated at RTL**, and both sit on the same primitive: a
[`LockedT2pMemIF`](../../interface/primitive/bram.md) — a lock channel over one true-dual-port BRAM,
which hands a *region* of the memory from one task to the other. That is what replaces the streaming
family's whole reverse-channel apparatus, and it is why the two halves of this family have the same
shape.

They differ in how many regions they ask for, and that difference is not cosmetic:

- **`RfShotTx` holds one region** and hands it back and forth. A load and a play therefore share
  addresses, in turn, and every handover costs a gap in the output.
- **`RfShotRx` holds two** and alternates. The writer and the reader are never in the same region, so
  there is nothing to hand over and nothing to gap.

The consequence is measured rather than argued, and it is on both pages.

## Where to start

- [**Transmit — `RfShotTx`**](./tx.md) — what to write to play a waveform: the ports, the in-band
  command, the five verdicts, the two play modes, and the rules that bite.
- [**Receive — `RfShotRx`**](./rx.md) — what to write to capture: the window header, what
  `n_dropped` means, and why two regions make loss impossible rather than merely unlikely.
- [**Internals**](./tx_internal.md) — for developers and agents. The tasks, the channels, the lock
  protocol, the on-wire layouts and the findings that are easy to rediscover the hard way. **Skip it
  if you only want to use the design.**

## Related

- [Choosing a sample buffer](../choosing.md) — the one question that decides between this family and
  the streaming one, and what each gives up.
- [Rfdc](../rfdc/) — the converter underneath, and the raw AXI-Stream interface this family sits on.
