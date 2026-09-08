---
title: Options and what is not built
parent: RfShotBuf
grand_parent: RF converters
nav_order: 4
audience: python
summary: "Where RfShotTx sits in the shot-buffer design space, and why today's point is the intersection of two constraints rather than a design in its own right. Covers what fixed-size relative indexing costs you, what absolute indexing would buy, what scoping found would make it cheap, and separately how much of the simulator's timing you are entitled to believe."
---

# Options and what is not built

`RfShotTx` sits at one point in a small design space, and **the point it sits at today is temporary**.
This page says which point, what the alternatives would buy, and what you are entitled to believe
about the simulator's timing — the last of which is a separate question about the *model* rather than
the design.

## Sizing and indexing are one choice, not two

How long a shot is and what an address *means* look independent and are not. An address can carry
absolute phase — *sample j lives at `mem[j mod BUF_LEN]`* — only if there is a fixed `BUF_LEN` to take
the modulus against. Let the length vary and the modulus has no fixed base, so the address can only
mean *wherever the host put it*.

That leaves three coherent designs, not four:

| | shot size | what an address means | status |
|---|---|---|---|
| **sounding** | fixed at build time | **absolute** — sample *j* is at `mem[j mod depth]` | not built |
| **general** | chosen per shot | **relative** — wherever it was loaded | not built; needs an allocator |
| **today** | fixed at build time | **relative** | **built** |

### Today is the intersection, and that is worth saying plainly

The bottom row takes the constraint of the first design and the guarantee of the second. **You accept
that a shot must be exactly `depth` words, and you get nothing back for it that a variable-length
design would not also give you.**

Fixed length is not free of value — it is what lets the load loop reach `II=1` with a counted trip
count, what gives the pad a length to pad *to*, and what keeps an allocator out of the design. But
those are *implementation* benefits. From where you sit they are not features, and the honest reading
is that this row is a way-station rather than a destination.

**The direction is toward the top row.** Absolute indexing needs the fixed length this design already
has, so it is a small change from here and variable sizing is a much larger one — see
*What scoping found* below.

## What absolute indexing would buy

**Memory index becomes a timestamp.** If transmitted sample *j* sits at `mem[j mod depth]` and a
received sample *j* likewise, then TX and RX correlate **by address** — no timestamping, no
bookkeeping. For channel sounding that correlation *is* the measurement.

### What scoping found

Two things, and both were better than expected.

**The player already carries the counter.** It writes `blk_words` words on *every* firing — samples
when it has something to play, `FILLER` when it does not — so its own output count is the absolute
word index. Today's read pointer is reset to `0` whenever a shot is accepted, which is the only reason
it is a *relative* index. Let it free-run and it is an absolute one.

**Starting at the beginning is still possible.** The obvious cost of absolute indexing — that playout
begins wherever the counter happens to be, so the first sample out is not the first sample of your
waveform — goes away if the design defers the change from filler to samples until the pointer reaches
a buffer boundary. `blk_words` already divides `depth`, so that boundary is hit exactly once per pass
and the wait is bounded by one pass. Because the waveform fills the whole buffer *and* starts on a
boundary, sample *j* is then always emitted at an absolute index congruent to *j* — the full
timestamp property, with *a waveform starts at its beginning* kept intact.

{: .note }
It would also survive the loosely-timed model below better than it looks. The two backends would each
start on a boundary of their **own** counter, so they would disagree about **which pass** a shot lands
in and agree exactly about **phase within the pass**. Phase is what a sounding correlation uses.

### What it would still depend on

**Tile synchronisation, which is not something this design can promise.** `Rfdc` already models the
gap: `t0_tx` is *"normally equal to `t0_rx` — that is what MTS gives you"*, and a non-zero value means
a tile deliberately started late, or a measured MTS residual. Absolute indexing is a property of the
buffer **and** the converter's epochs agreeing, and only the first half lives here.

{: .note }
It would need its **own** gate, not a stricter version of the current one. Today's phase check asserts
`real[i] == shot_codes[i % nsamp]` *within a playout segment*; the absolute version asserts against a
**global** sample counter. A different assertion, not a tightening.

## A separate question — how faithfully simulation reproduces timing

This one is about the model, not the design, and it is orthogonal to everything above.

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

**Fixed-size shots, relative indexing, loosely timed.** That is what every gate exercises and what the
[transmit](./tx.md) and [receive](./rx.md) pages describe.

The alternatives above are coherent designs and none is implemented. If one of them is what you need,
that is worth knowing before you build on this rather than after.

## Next

- [Transmit — `RfShotTx`](./tx.md) — the design as built.
- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — how the tasks share the memory, and why the transient is what it is.
- [Choosing a sample buffer](../choosing.md) — whether this family is the right one at all.
