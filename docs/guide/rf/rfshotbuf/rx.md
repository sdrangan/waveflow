---
title: Receive — RfShotRx
parent: RfShotBuf
grand_parent: RF converters
nav_order: 2
audience: python
api: [RfShotRx, CaptureWindowHdr, Rfdc, RFSampIF, StreamIF]
summary: "Capturing samples out of a converter into a memory a reader drains behind it: RfShotRx fills one region while a reader drains the other, so nothing is dropped while the reader keeps up. The boundary ports, the window header as a field table, what n_dropped and CAP_LOST each answer and why both are needed, and the two rules an ADC-facing design cannot break — it may not stall, and it may not overwrite a region nobody has read."
---

# Receive — `RfShotRx`

`RfShotRx` is the **receive half** of the finite sample buffer. It captures continuously out of a
converter into a BRAM split into **two regions**: it fills one while a reader drains the other, and
swaps. Every completed region goes out as one frame — a header, then the samples.

It is what you use when samples arrive faster than you want to move them, and you want to move them
in blocks rather than one at a time. It is *not* a triggered oscilloscope capture with pre-trigger
history — see [what this is not](#what-this-is-not).

```
   Rfdc.rx_streams[0] ──slots──▶ ┌──────────────────┐
                                  │ RfRelayoutToDense│
                                  └────────┬─────────┘
                                           │ dense words
                                           ▼
                                  ┌──────────────────┐
                                  │  PingPongCapture │──┐
                                  └────────┬─────────┘  │ lock ⇄ region A
                                           │ rdy        ▼
                                           │      ┌──────────┐
                                           │      │   BRAM   │
                                           ▼      └──────────┘
                                  ┌──────────────────┐  ▲
              w_out ◀─────────────│  PingPongWindow  │──┘ lock ⇄ region B
                                  └──────────────────┘
```

**The re-layout is first here and last on TX**, and that is not an arranged symmetry: the memory holds
*dense* words on both sides, because dense is the logic-side format a host can read and write without
knowing anything about slot justification. The conversion happens wherever the converter is.

## Why two regions, when TX has one

On transmit, a handover is a **gap** — the converter plays filler for as long as the swap takes, and
you had already accepted discontinuity when you asked to change waveform.

On receive there is no such option. **You cannot back-pressure an ADC.** A reader holding the region
the capture needs is not a gap; it is samples that no longer exist. Two disjoint regions are what
make *nothing is dropped* reachable at all — the writer and the reader are never in the same region,
so there is nothing to hand over and nothing to lose in the handing.

That difference is measured, not argued: the RTL gate scans the memory's own pins and finds
**140 cycles with both ports live and 0 with the writer and the reader inside the same region**
(`tests/examples/test_rf_shot_rx_xsi.py`). On TX the same scan finds collisions, benign but non-zero,
because TX holds one region — see [the internals page](./tx_internal.md#finding-tx-holds-one-region-rx-holds-two).

## The lock arbitrates; it does not synchronise

There is a `rdy` channel between the capture and the reader, and its existence is a **finding**
rather than an oversight.

The lock answers *may I touch these addresses*. It has no way to say *there is something there worth
touching*. A reader that alternated blindly would acquire a half the capture had not filled yet and
drain zeros — the plausible-samples failure this whole family keeps meeting. So the capture
**announces** each region as it completes it, and the reader blocks on that announcement before it
asks for anything.

One channel, one word, and the word is the region's **base address** — not an index, because an index
would be a second encoding of a geometry the lock already speaks in addresses.

## Instantiating one

```python
from waveflow.hw.rf_shot_rx import RfShotRx, N_REGION
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord

word = Rfsoc4x2SampWord.specialize(samp_per_word=4)
rfdc = Rfdc(name="rfdc", sim=sim, n_rx=1, n_tx=0, word=word)

dut = RfShotRx.for_word(
    word,
    depth=256,        # words in the memory, split into N_REGION (= 2) regions
    blk_words=16,     # words per converter block: the chunk, the poll period, the output burst
    sim=sim, name="dut", clk=axis_clk,
)
```

`depth` **must be a power of two** — the address wrap is a mask — and it is split evenly into
`N_REGION` regions, so a window is `depth // 2` words. `blk_words` is one number serving four roles
(the re-layout's burst, the capture's chunk, its poll period, and the reader's output burst) because
they are one quantum: the converter's block.

## The boundary

| port | direction | carries |
|---|---|---|
| `samp_in` | in | samples, slot-packed, straight from `Rfdc.rx_streams[0]` |
| `w_out` | out | one frame per completed window: a `CaptureWindowHdr`, then the samples |

As on TX, the memory is inside the design and its two wires never leave the wrapper.

## The window header

One 64-bit word, ahead of each window's samples on the same stream.

| field | bits | meaning |
|---|---|---|
| `status` | 8 | `CAP_OK` or `CAP_LOST` |
| `base_addr` | 28 | first element of the region this window came from |
| `n_dropped` | 28 | words lost **since reset** — cumulative, wraps at 2²⁸ |

**Both fields are there, and they answer different questions.**

`n_dropped` is **cumulative, never incremental**. That follows the reverse-channel rule: a lost
cumulative value is harmless, because the next one carries the whole truth; a lost *increment* is
wrong forever.

`status` answers *was anything lost immediately before this window?* — which is the actionable
question and is **not derivable** from one cumulative reading. A host would have to remember the last
value and subtract. The design already knows, so it says so. It is the same split `ShotTxResp` makes
between a `status` and an `nsamp_loaded`.

## The two rules an ADC-facing design cannot break

**1. It will not stall.** Every firing the capture takes a block off its input, whatever else is
happening. A task that back-pressured an ADC would be modelling something that cannot exist.

**2. It will not overwrite a region nobody has read.** It writes a block only into a region that is
*free* — not yielded to the reader, and not still holding samples nobody has drained.

Those two together are what produce a **drop**: when there is no free region, the block is discarded
and counted. That is the honest failure of a capture whose reader fell behind, and it is the only one
available — the alternatives are stalling the ADC (impossible) or silently corrupting a window
somebody is reading (worse).

**A dropped block is otherwise perfectly silent**, which is why the count is on the wire rather than
in a Python attribute. `RfShotRx.assert_no_loss()` makes it loud in simulation.

**The strongest statement of "nothing was dropped" is not the counter.** It is that the windows,
concatenated, are *contiguous* — drive a ramp in, and a gap in the numbers is a gap in the capture,
with no counter to be believed. That is `RfShotRx.assert_windows_contiguous()`, and it is what the
RTL gate asserts: **640 words captured, 0 dropped, across 40 converter blocks**
(`tests/examples/test_rf_shot_rx_xsi.py`).

## What this is not

**It is not pre-trigger capture.** `RfShotRx` captures continuously and drops nothing while the
reader keeps up, but the history you can read back is **one region deep** — it hands out each region
as it completes it and immediately begins overwriting the other. Arming a capture, running until a
trigger, and then reading back the *whole memory ending at the trigger* is a different design, and it
is specified in `plans/rf_shot_buf.md` Stage C. It is **not built**.

The distinction matters because the shot family is the only family that *can* do pre-trigger — a
continuous buffer has already discarded the samples — so it is easy to assume the capability arrived
with `RfShotRx`. It did not.

## Next

- [Transmit — `RfShotTx`](./tx.md) — the other half of the family.
- [Internals](./tx_internal.md) — the tasks, the lock protocol, and the measurements. You do not need
  it to use this design.
- [Choosing a sample buffer](../choosing.md) — whether this is the right family at all.
- The worked example: `examples/rf_shot_rx/`, with its pysim golden and its RTL gate.
