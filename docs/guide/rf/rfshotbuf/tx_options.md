---
title: Options and what is not built
parent: RfShotBuf
grand_parent: RF converters
nav_order: 4
audience: python
summary: "Two independent choices a shot buffer makes — how the memory is indexed, and how faithfully simulation reproduces timing — with what is built today, what is planned, and what each alternative would buy. Read this if you are deciding whether RfShotTx fits, or wondering why a transient does not match."
---

# Options and what is not built

`RfShotTx` makes two choices that could each have gone the other way. Both are worth knowing before
you build on it: one decides whether you can correlate transmitted and received samples by memory
address, and the other decides how much of the simulator's timing you are entitled to believe.

**Only one setting of each is built.** The others are described here so you can tell whether the
design fits, rather than discovering the limit later.

## Axis 1 — how the memory is indexed

The buffer length is **fixed at build time** either way: `nword` is a build parameter, not a header
field, and a command that disagrees is refused with `SHOT_WRONG_LEN`. What differs is what an address
*means*.

| | **relative** (built) | **absolute** (not built) |
|---|---|---|
| where sample *i* of a shot lives | `mem[base + i]` | `mem[i mod BUF_LEN]` |
| where playout restarts | the region's start, every pass | wherever the sample counter says |
| TX↔RX relation | none | **sample *j* is at the same index in both** |
| depends on | nothing outside the design | `t0_tx ≡ t0_rx` — MTS actually holding |

### What absolute indexing would buy

**Memory index becomes a timestamp.** If transmitted sample *j* sits at `mem[j mod BUF_LEN]` and a
received sample *j* likewise, then TX and RX correlate **by address**, with no timestamping and no
bookkeeping. For channel sounding that correlation *is* the measurement.

### What it would cost

The guarantee holds only as far as **tile synchronisation** does. `Rfdc` already models this:
`t0_tx` is *"normally equal to `t0_rx` — that is what MTS gives you"*, and a non-zero value means a
tile deliberately started late, or a measured MTS residual. So absolute indexing is not a property of
the buffer alone; it is a property of the buffer **and** the converter's epochs agreeing.

It also gives up the freedom relative indexing has: with an absolute index, where a shot sits is
decided by its sample number rather than by you.

{: .note }
Absolute indexing would need its **own** gate, not a stricter version of the current one. Today's
phase check asserts `real[i] == shot_codes[i % nsamp]` *within a playout segment*; the absolute
version asserts against a **global** sample counter. Different assertion, not a tightening.

## Axis 2 — how faithfully simulation reproduces timing

| | **loosely timed** (built) | **matched** (not built) |
|---|---|---|
| what paces the player | back-pressure from the converter | an explicit timing event from `Rfdc` |
| steady-state output | identical to RTL | identical to RTL |
| **startup and handover transients** | **bounded, not identical** | identical |
| used for | every gate | debugging a value mismatch |

### What loosely timed means for you

**The samples are right; when the first one appears is approximate.** In simulation the design plays
the same values, in the same order, for the same number of passes as the hardware does. What differs
is how much filler precedes the first real sample, and by how much a handover gap differs — both
bounded by the buffering between the player and the converter, and neither accumulating over a run.

If you are asking *"does the right waveform come out"*, simulation answers exactly. If you are asking
*"when does my first sample hit the air relative to a trigger"*, simulation answers **approximately**,
and the bound is what you are entitled to.

This is the ordinary loosely-timed trade: transients are the first thing an LT model gives up, and it
buys the speed that makes block-rate simulation practical.

### What matched mode would buy

A mode in which the two backends' transients coincide exactly, so a **value** difference is not hidden
behind a **timing** difference. That is a debugging tool rather than a fidelity claim — you would
switch to it when a comparison fails and you want the transient out of the way.

The gates would still run loosely timed. A defect visible only in matched mode may not exist in the
mode that ships.

## What is built today

**Relative indexing, loosely timed.** That combination is what every gate exercises and what the
[transmit](./tx.md) and [receive](./rx.md) pages describe.

The other three combinations are coherent designs; none is implemented. If one of them is what you
need, that is worth knowing before you build on this rather than after.

## Next

- [Transmit — `RfShotTx`](./tx.md) — the design as built.
- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — how the tasks share the memory, and why the transient is what it is.
- [Choosing a sample buffer](../choosing.md) — whether this family is the right one at all.
