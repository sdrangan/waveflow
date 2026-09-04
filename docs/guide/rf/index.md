---
title: RF converters
parent: Guide
nav_order: 10.25
has_children: true
audience: python
summary: "Modelling a design that talks to one or more RF data converters — ADC/DAC blocks such as the RFDC on an AMD RFSoC. Rfdc emulates the converter itself, with an AXI-Stream side identical to the AMD IP and an RF side you can attach sources, sinks and channels to; the RfShotBuf and RfStreamBuf families sit on top of it when you want to hold samples rather than pass them through."
---

# RF converters

Many applications, wireless ones especially, connect to one or more RF data converters — ADC/DAC
blocks such as the RFDC on an AMD/Xilinx RFSoC part. Waveflow provides a simple method for modelling
and interfacing with such converters.

[**`Rfdc`**](./rfdc/) is Waveflow's emulation of the converters themselves. It is modelled to have an
AXI-Stream interface identical to the
[AMD RF Data Converter IP](https://docs.amd.com/r/en-US/pg269-rf-data-converter/Introduction), plus a
simulation of the interface to the RF environment. The RF environment lets you run sources, sinks,
nodes and channels directly in your simulation and observe how your logic behaves.

`Rfdc` exposes raw AXI streams for each data converter path, with intricate packing and back-pressure
rules. You are free to build logic against those streams directly — for anything that processes
samples as they arrive, a filter or a detector, that is the right answer. Waveflow also provides two
hardware modules that sit on top and offer a simpler, asynchronous interface:

- [**`RfShotBuf`**](./rfshotbuf/) — the **finite** family, for designs where nothing reads while
  something is writing: load a waveform, *then* play it; capture into a region, *then* transfer it.
  All of its memory is payload, and it is the only family that *could* give you pre-trigger history.
  Two classes: [`RfShotTx`](./rfshotbuf/tx.md) and [`RfShotRx`](./rfshotbuf/rx.md).

- [**`RfStreamBuf`**](./rfstreambuf/) — the **continuous** family, for designs where the reader and
  the writer overlap: load the next waveform while the current one plays, or drain a capture while
  still capturing. Unbounded in duration, at the cost of headroom and a reverse channel. Two classes:
  `RfTxStream` and `RfSampBufRx`.

Both names are **families rather than classes** — there is no `RfShotBuf` or `RfStreamBuf` to
import.

See [choosing a sample buffer](./choosing.md) for more detail on selecting between them — it turns
the choice into one question you can check against your own design.
