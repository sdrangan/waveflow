---
title: RfStreamBuf
parent: RF converters
nav_order: 3
audience: python
summary: "The continuous sample buffer: for designs where the reader and the writer overlap. A family name covering RfTxStream on transmit and RfSampBufRx on receive; the transmitter is finished and RTL-gated, the receiver is the older BRAM design its replacement has not reached yet. Not yet written."
---

# RfStreamBuf

**Under construction.**

`RfStreamBuf` is the **continuous** sample buffer — load the next waveform while the current one
plays, or drain a capture while still capturing. It buys unbounded duration and data you can change
mid-flight, and pays for them with headroom, a reverse channel, and a strictly larger failure
surface.

> **`RfStreamBuf` is a family name, not a class.** There is no `RfStreamBuf` to import, and there
> never has been. The family is two concrete modules, and unlike
> [the shot family](../rfshotbuf/) they are **not at the same stage**:
>
> | | class | module | status |
> |---|---|---|---|
> | **transmit** | `RfTxStream` | `waveflow/hw/rf_tx_stream.py` | built, RTL-gated by `tests/examples/test_rf_circ_play_xsi.py` |
> | **receive** | `RfSampBufRx` | `waveflow/hw/rf_samp_buf.py` | built and RTL-gated, but it is the **older** BRAM-and-progress-channel design |
>
> `RfTxStream` is the finished, stream-based transmitter (`plans/rf_samp_new.md` Stage 1). The
> stream-based *receiver* that would replace `RfSampBufRx` is Stage 2 of that plan and is **not
> built**, so `RfSampBufRx` is what you use today. Read `plans/rf_samp_new.md` before building on
> its internals.

Until this section exists, the material that covers it is:

- [Choosing a sample buffer](../choosing.md) — what this family costs that the shot family does not.
- [Rfdc](../rfdc/) — the converter underneath, and the raw AXI-Stream interface this family sits on.
