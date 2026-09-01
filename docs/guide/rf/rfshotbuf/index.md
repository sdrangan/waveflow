---
title: RfShotBuf
parent: RF converters
nav_order: 2
has_children: true
audience: python
summary: "The finite sample buffer: for designs where nothing reads the memory while something else is writing it. Not yet written."
---

# RfShotBuf

**Under construction.**

`RfShotBuf` is the **finite** sample buffer — load a waveform, *then* play it; capture a window, *then* transfer it. Because the writer and the reader never overlap there is nothing to arbitrate, so all of its memory is payload and it is the only one of the two that can give you pre-trigger history.

> **Status: designed, not built.** See the status note on [choosing a sample buffer](../choosing.md).

Until this section exists, the material that covers it is:

- [Choosing a sample buffer](../choosing.md) — the one question that decides between this and `RfStreamBuf`.
- [Rfdc](../rfdc/) — the converter underneath, and the raw AXI-Stream interface this buffer sits on.
