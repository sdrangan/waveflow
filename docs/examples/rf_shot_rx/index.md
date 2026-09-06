---
title: Capturing without losing anything
parent: Examples
nav_order: 9.7
has_children: true
audience: python
summary: "The worked example for RfShotRx: a real ADC plays a ramp into a memory split into two regions, the capture fills one while the reader drains the other, and every window goes out as a frame with a header that says what was lost. The scenario IS the gate — a ramp makes a dropped block a visible step in the numbers rather than something a counter has to be believed about."
---

# Capturing without losing anything

`examples/rf_shot_rx` is the worked example for [`RfShotRx`](../../guide/rf/rfshotbuf/rx.md) — the
receive half of the finite sample buffer. An ADC plays samples in continuously; the design captures
them into a BRAM split into **two regions**, filling one while a reader drains the other, and hands
out each completed region as one frame.

**This page is about the example.** What the design *is* — the two regions, the `rdy` announcement,
what `n_dropped` and `CAP_LOST` each answer — is [the guide's](../../guide/rf/rfshotbuf/rx.md).

```
RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_streams[0] --> RfShotRx.samp_in
RfShotRx.w_out --> StreamSink        (one FRAME per window: a header, then the samples)
```

## The converter is really here, and the tile is ADC-only

`n_rx=1, n_tx=0`. The one thing a capture design exists to satisfy is that **an ADC cannot be told to
wait**, and the whole claim is that a window read-out does not make it wait either — so the converter
has to be in the graph rather than modelled away. Wiring a fake DAC in would add a metronome nothing
feeds.

## There is no command stream

A capture is asked nothing. It is told *when a region is ready* by the design itself, and it answers
on every window with a header a host can act on. Compare
[`examples/rf_samp_buf_rx`](../../guide/rf/choosing.md), whose whole middle is a command layer,
because *its* reader has to say which window it wants.

## The scenario is the gate

The source plays a **ramp** of converter codes. So the windows the host receives must concatenate
into a contiguous ramp — and a dropped block is a **step in the numbers**, visible whether or not
anything counted it.

That is what makes *nothing was lost* checkable rather than merely reported. The strongest statement
in this example is not the drop counter; it is
`test_the_rtl_captures_the_ramp_with_no_gap`, which needs no counter to be believed. The header's
`n_dropped` and `status` are asserted too, because the two agreeing is what says the design knows
what it lost.

## The geometry

| | value | what it is |
|---|---|---|
| `depth` | 256 words | the memory, split into `N_REGION = 2` |
| region | 128 words | one window — `depth / 2` |
| `blk_words` | 16 | words per converter block: the chunk, the poll period, the output burst |
| `samp_rate` | 256 MSa/s | the ADC's grid |
| run | 40 blocks | long enough to fill several regions and hand out several windows |

`depth` **must be a power of two** — the address wrap is a mask — and `blk_words` is one number
serving four roles because they are one quantum: the converter's block.

## What it measures

| measurement | value | asserted by |
|---|---|---|
| ADC words captured | **640** | `test_the_converter_never_had_a_word_refused` |
| words dropped | **0** | *same*, and `test_every_window_carries_CAP_OK_and_zero_lost` |
| converter blocks | 40 | `test_the_converter_never_had_a_word_refused` |
| window words delivered | 516 | `test_the_host_got_the_recorded_words_on_the_recorded_cycle` |
| last window at cycle | **2205** | *same* |
| both memory ports live | 140 cycles | `test_both_ports_are_live_together_and_never_in_the_same_region` |
| writer and reader in the **same region** | **0** | *same* |

**The last two are the pair that matters**, and they are why this design has two regions where
[TX has one](../../guide/rf/rfshotbuf/tx_internal.md#finding-tx-holds-one-region-rx-holds-two): the
ports are busy together for 140 cycles — that is a true-dual-port memory doing its job — and they are
never once inside the same region. On TX the same scan finds collisions, benign but non-zero. Here
the question does not arise, **by construction** rather than by measurement.

## Pages

- [**Running it**](./run.md) — the build rungs, and what each produces.
- [**Taking it to RTL**](./rtl.md) — the RTL run and every measured number, with its gate.

## Related

- [`RfShotRx`](../../guide/rf/rfshotbuf/rx.md) — the design: the ports, the window header, the two
  rules an ADC-facing design cannot break.
- [`examples/rf_shot_tx`](../rf_shot_tx/) — the transmit half of the same family.
- [Choosing a sample buffer](../../guide/rf/choosing.md) — whether this family is the right one.
