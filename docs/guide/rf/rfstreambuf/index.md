---
title: RfStreamBuf
parent: RF converters
nav_order: 3
audience: python
summary: "The continuous sample buffer: for designs where the reader and the writer overlap. Not yet written."
---

# RfStreamBuf

**Under construction.**

`RfStreamBuf` is the **continuous** sample buffer — load the next waveform while the current one plays, or drain a capture while still capturing. It buys unbounded duration and data you can change mid-flight, and pays for them with headroom, a reverse channel, and a strictly larger failure surface.

Until this section exists, the material that covers it is:

- [Choosing a sample buffer](../choosing.md) — what this buffer costs that `RfShotBuf` does not.
- [Rfdc](../rfdc/) — the converter underneath, and the raw AXI-Stream interface this buffer sits on.
