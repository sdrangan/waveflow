---
title: Playing a stored shot
parent: Examples
nav_order: 10.7
has_children: true
summary: "A host loads one waveform over an AXI-Stream frame, gets one verdict back, and the design plays that waveform three times out of a real converter. The same user story as the repeat player built on the streaming transmitter — and deliberately built a second time, on the finite buffer, so the comparison between the two buffer classes is checkable rather than asserted. Along the way: the first boundary port in this repo with a real TLAST pin, and why a short transfer is a verdict here instead of a hang."
---

# Playing a stored shot

A waveform goes in once. It comes out three times, on the converter's grid, and the host is told
whether the load was any good.

```
DMA (MM2S) --AXIS: [ShotTxHdr | dense words ... TLAST]--> RfShotTx.s_in
DMA (S2MM) <------------------ ShotTxResp -------------- RfShotTx.resp_out
                                            RfShotTx.samp_out --> Rfdc.tx_streams[0]
                                                       Rfdc.tx_rf --RFSampIF--> the air
```

That is the whole external interface: **one stream in, one stream back, one stream to the
converter.** Everything else is inside.

## Why this exists when `rf_repeat_play` already did it

[`rf_repeat_play`](../../guide/rf/) answers the same question — *load a waveform, replay it* — with
the **streaming** transmitter. It needs an absolute slot grid, a pending FIFO, an ack channel, a
lateness verdict per window, and a scheduler that has to *ask the hardware where "now" is* because
nothing else can tell it.

This design has none of those. The comparison is the point:

| | streaming (`rf_tx_stream`) | shot (`rf_shot_tx`) |
|---|---|---|
| how a play is scheduled | an absolute slot per window, computed by a scheduler | it isn't — the converter back-pressures |
| what comes back per window | a `TxStatus` on an ack channel, then a `TxResp` | nothing; one verdict per *load* |
| how "now" is learned | by issuing `start_now` and reading the answer | not needed |
| duration | unbounded | bounded by the memory |
| what a late window costs | `TX_TOO_LATE`, and a hole in the playout | not reachable |
| pre-trigger history | deleted by construction | available (that is Stage C) |

[`docs/guide/rf/choosing.md`](../../guide/rf/choosing.md) divides the two buffer classes on **one**
question: *does anything read the buffer while something else is writing it?* Building the same user
story both ways is what turns that division from a claim into something you can read off two
diagrams — and the diagrams really are that different.

**What the two designs share** is the converter, the sample rate per fabric cycle, and the II. Both
run their datapaths at one word per cycle. So the simplification did not cost throughput; it cost
the ability to play longer than the memory, which is the row above that says *bounded*.

## Inside

Five `hls::task`s and one memory beside them:

```
s_in --> ShotTxLoad --pay--> RfShotBufLoad --> [ BRAM ] --> RfShotBufRead --> RfRelayoutToSlots
          |    ^ done              |                                                  |
     resp_out  |                  rdy                                                samp
               +--rep--> ShotTxPlay <---------------------------------------------------+
                              |
                              +--> samp_out --> the converter
```

Three of those five are **Stage A's, used exactly as they were built and gated** — the buffer's
writer, its reader, and the re-layout that turns the logic-side format into the converter's. Nothing
in `waveflow/hw/rf_shot_buf.py` or `waveflow/hw/rf_relayout.py` changed to make this example.

What this stage added is the two ends: a **loader** that reads a header and answers it, and a
**player** that turns one loaded shot into `nrepeat` plays.

### The one reverse channel, and what it is not

There is exactly one signal going backwards inside the design: the player's `done` token. It is not
arbitration — the reader and the writer of this buffer are never live at the same time, so there is
nothing to arbitrate. It answers one question: *may I overwrite the memory yet?* A load that arrives
before the answer is **refused**, and refused at the command, before a single payload word is taken.

That refusal is the design's whole concurrency story, and it is one bit.

## The geometry this example is built and measured at

Stated rather than defaulted, because every recorded number below belongs to exactly this:

| | |
|---|---|
| converter word | RFSoC 4x2: four 14-in-16 samples in a 64-bit beat |
| shot | 64 words = **256 samples** |
| memory | 256 words = 1024 samples (a shot is deliberately *shorter* than the memory) |
| plays | 3 |
| sample rate | 256 MSa/s — **0.256 words per fabric cycle** at 250 MHz |
| converter block | 64 samples, so one shot is exactly four blocks |

Four samples per beat rather than one is not a detail: it makes the converter's `justify_shift`
non-zero, so the re-layout stage is a real conversion instead of a pair of wires. A build that got
that wrong would be measuring the identity, and the build refuses to run at `shift = 0` for exactly
that reason.

## Pages

1. [Running it](run.md) — the two scenarios, the five verdicts, and the one a DMA cannot give.
2. [Taking it to RTL](rtl.md) — the boundary port that grew a `TLAST` pin, the achieved II, and the
   recorded cycle counts.

## What is not here

**No transfer-time number.** How long a host takes to push a shot over PYNQ depends on the DDR it
comes from, and **neither RFSoC memory is calibrated** in this repo — `waveflow/calib/platforms/`
holds one entry and it is not this board. So the load time is *uncalibrated*, and saying so is the
honest answer; every number on these pages is measured downstream of the stream port, where a cycle
count means something.

**No capture.** Reading samples *back* — triggered capture with pre-trigger history, the one thing
the streaming design cannot do at all — is the next stage, and it chooses its own transport with
capture evidence in hand rather than inheriting this one's.
