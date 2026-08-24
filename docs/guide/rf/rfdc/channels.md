---
title: Multi-channel support
parent: Rfdc
grand_parent: RF converters
nav_order: 1
audience: python
api: [n_rx, n_tx, n_ch, RFSampIF, Rfdc]
summary: "Configuring how many receive and transmit datapaths a converter has, and what a channel costs on each of its two sides: one AXI-Stream port per datapath on the fabric side, matching AMD's channelization, and one RFSampIF per direction on the RF side carrying every channel of that direction in a single block. Also what you would have to do by hand to lower a multi-channel design onto real RFDC blocks, and why the word tile does not mean here what it means in AMD's documentation."
---

# Multi-channel support

## Configuring the number of channels

One Waveflow `Rfdc` represents multiple RF-ADC and RF-DAC datapaths. How many is set by two
parameters in the [configuration](./converter.md):

- **`n_rx`** — RF-ADC datapaths, for receive (RX)
- **`n_tx`** — RF-DAC datapaths, for transmit (TX)

`n_rx = 0` (or `n_tx = 0`) is **not** an error: that is a transmit-only or receive-only converter, and
the absent path simply has no process, no rate check and no BFM model. Its endpoints still exist,
unbound, which costs nothing and keeps the endpoint set a property of the class rather than of a
build.

## Interfaces

![One Rfdc containing an n_rx ADCs block and an n_tx DACs block. On the fabric side, rx_streams[0] through rx_streams[n_rx-1] and tx_streams[0] through tx_streams[n_tx-1] are separate AXI-Stream lines, one per channel. On the RF side a single RFSampIF per direction carries every channel of that direction as one (n_ch, blksize) block, and continues to the rest of the RF environment.](../figures/rf_channels.svg)

The two sides of an `Rfdc` count channels differently, and each takes the form its consumer wants:

| side | shape | why |
|---|---|---|
| **fabric** | `n_rx` AXI-Stream **master** ports + `n_tx` **slave** ports, one per channel | it is what the IP presents, and one wide interleaved port would push a vendor packing rule into every design that touches a converter |
| **RF** | **one** `RFSampIF` per direction, carrying every channel of that direction in one block | the RF environment and your logic both want the channels **together**; splitting it would give `n_ch` events per block period, against the whole point of block-rate modelling |

### `n_ch` is the same number {#one-number-named-for-its-object}

The RF edge names its channel count **`n_ch`**; the converter names its **`n_rx`** and **`n_tx`**.
They are the same number — `n_ch == n_rx` on the RX edge, `n_ch == n_tx` on the TX edge — and each
name sits on the object it belongs to. `Rfdc` reads the edge's value at bind and refuses a
disagreement, rather than picking a winner.

## Binding the ports

Waveflow uses **one AXI-Stream per RF-ADC/DAC datapath**, following the same convention as the AMD
converters. In the Python model `rx_streams` and `tx_streams` are therefore lists, and you index them
when binding — **even when there is only one path**:

```python
adc_axis.bind("master", rfdc.rx_streams[0])     # even with n_rx == 1
```

One spelling, no special case for the channel count that every example happens to use.

## Future Vivado lowering

Connecting Waveflow-generated logic to real AMD RFDC blocks in Vivado is **manual today**. You
replace the Waveflow `Rfdc` with the corresponding AMD RFDC blocks and wire your logic to them; the
AXI-Stream interfaces are designed to match AMD's channelization and bit packing, so the fabric side
lines up (see the [AXI formatting notes](./axis_side.md)). You then configure the converters from the
host by writing their AXI-Lite registers.

Two things Waveflow does **not** do: it does not model the AXI-Lite configuration of the RFDC blocks,
and it does not generate a Vivado project wired to them. Both may come later.

## Tiles

Some of the Waveflow documentation uses the word *tile* — **not in AMD's sense of it.**

In the AMD IP a tile is a group of **same-direction** converters sharing a clock and a power-up
sequence: a Quad RF-ADC tile holds four RF-ADCs (in two pairs, each pair configurable for I/Q), a Dual
tile holds two, and RF-ADC and RF-DAC tiles are separate. A Waveflow `Rfdc` carries **both**
directions, so it spans an ADC tile and a DAC tile, and `n_rx` need not be a whole tile's worth.

What the model does borrow from the tile is the **epoch**. `t0_rx` and `t0_tx` are each *a tile's*
sample counter starting, and they are two separate numbers precisely because the ADC and the DAC are
two separate tiles — started separately, and often clocked at different rates.

## Next

- [Real and I/Q](./iqmode.md) — what a *sample* on a channel is, and the two flags that say so.
- [Quickstart](./quickstart.md) — a converter wired end to end.
- [The RF side](./rf_side.md) — where `n_ch`, `blksize` and the sample clock are declared.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `waveflow/hw/rf_sample_if.py`,
`plans/adc_model.md` § *Channels, ports, and where I/Q lives*.
