---
title: Options and what is not built
parent: RfShotBuf
grand_parent: RF converters
nav_order: 4
audience: python
summary: "Where RfShotTx sits in the shot-buffer design space. Covers absolute indexing -- built for TX as the build-time `absolute_index`, what it buys, what one pass of latency it costs, and why it is only half of the sounding story while RfShotRx does not index absolutely -- what fixed-size relative indexing still costs you, and separately how much of the simulator's timing you are entitled to believe."
---

# Options and what is not built

`RfShotTx` sits at one point in a small design space, and **the point it sits at today is temporary**.
This page says which point, what the alternatives would buy, and what you are entitled to believe
about the simulator's timing — the last of which is a separate question about the *model* rather than
the design.

## Fixed vs. variable shot size

**Sizing and indexing are one choice, not two.** How long a shot is and what an address *means* look independent and are not. An address can carry
absolute phase — *sample j lives at `mem[j mod BUF_LEN]`* — only if there is a fixed `BUF_LEN` to take
the modulus against. Let the length vary and the modulus has no fixed base, so the address can only
mean *wherever the host put it*.

That leaves three coherent designs, not four:

| | shot size | what an address means | status |
|---|---|---|---|
| **sounding** | fixed at build time | **absolute** — sample *j* is at `mem[j mod depth]` | **built on TX** (`absolute_index=1`); `RfShotRx` does not |
| **general** | chosen per shot | **relative** — wherever it was loaded | not built; needs an allocator |
| **default** | fixed at build time | **relative** | **built**, and the default |

### The default row is the intersection

The bottom row takes the constraint of the first design and the guarantee of the second. **You accept
that a shot must be exactly `depth` words, and you get nothing back for it that a variable-length
design would not also give you.**

Fixed length is not free of value — it is what lets the load loop reach `II=1` with a counted trip
count, what gives the pad a length to pad *to*, and what keeps an allocator out of the design. But
those are *implementation* benefits. From where you sit they are not features.

The top row is now reachable from here, on the transmit side, by building with `absolute_index=1`.
Variable sizing is still a much larger change and still needs an allocator.

## Absolute indexing — `absolute_index`

**Memory index becomes a timestamp.** With `absolute_index=1` the player's read pointer counts
*every* word it emits since reset — filler included — so the address a sample comes out of is that
sample's own word index modulo `depth`. Sample *j* is emitted at an absolute index congruent to *j*,
whenever the shot happened to be loaded.

```python
tx = RfShotTx.for_word(Rfsoc4x2SampWord.specialize(samp_per_word=4), sim=sim,
                       depth=64, blk_words=16, absolute_index=1)
```

It is a **build-time** parameter and not a runtime flag: it changes the RTL, so it is an `HwParam`
lowered as a template argument. `0` is the default and is exactly the behaviour every earlier version
of this design had.

### A playout starts at its own beginning

The obvious cost of absolute indexing would be that playout begins wherever the counter happens to be,
so the first sample out is not the first sample of your waveform. **Deferring the start to a buffer
boundary removes that instead of trading it.** `blk_words` divides `depth`, so the read pointer takes
exactly `0, blk_words, ... depth - blk_words` and hits zero once per pass; the player waits for that
zero before it switches from filler to samples. Because the waveform fills the whole buffer *and*
starts on a boundary, you get the timestamp property with *a waveform starts at its beginning* intact.

### What it costs: one pass of latency, and never more

A shot loaded partway through a pass waits for the next boundary before a single sample reaches the
air. That wait is bounded by one pass and is measured, not asserted: at the gated geometry
(`depth=64`, `blk_words=16`, four samples per word) the finite scenario's startup filler goes from 192
samples to 256.

**If your loads arrive closer together than one pass, some of them will never play at all.** A load
that preempts an armed-but-not-yet-started shot cancels it — correctly, since the memory it would have
played has already been rewritten. The gated infinite scenario is exactly that case: it plays two
waveforms under the default and **nothing** under `absolute_index=1`, on the same command stream. That
is the design working, and it is the number to look at before choosing this mode for a design that
switches waveforms quickly.

Nothing else moves. The converter is never starved — a longer wait is longer filler, which is a value
the design produces rather than a stall — every verdict on the response path is unchanged, and every
pipelined loop still reaches `II=1`.

### It survives the loosely-timed model

The player only ever writes whole chunks, so the loosely-timed lead is a whole number of chunks and
each backend starts on a boundary of its **own** counter. The two therefore disagree about **which
pass** a shot lands in and agree exactly about **phase within the pass** — measured: pysim starts the
gated shot at absolute sample 768 and the RTL at 256, and both satisfy the same congruence.

### What this does not give you

**It is the transmit half.** `RfShotRx` captures continuously into two regions and announces each with
a `base_addr` on the wire; whether *its* addresses can carry absolute phase is a separate question
with a different answer, because a capture that drops a block loses its place in a way a player
cannot. **Correlating TX against RX by address is not available**, and a page claiming otherwise while
only one end indexes absolutely would be worse than one that admits the gap.

**Tile synchronisation is not something this design can promise either.** `Rfdc` models the gap:
`t0_tx` is *"normally equal to `t0_rx` — that is what MTS gives you"*, and a non-zero value means a
tile deliberately started late, or a measured MTS residual. Absolute indexing is a property of the
buffer **and** the converter's epochs agreeing; only the first half lives here.

## Loosely timed vs. matched timing



**This one is about the model, not the design**, and it is orthogonal to everything above: it decides
how much of the simulator's timing you are entitled to believe, whichever design you are running.

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

**Fixed-size shots, loosely timed, and indexing you choose at build time.** `absolute_index=0` — the
default, and what the [transmit](./tx.md) and [receive](./rx.md) pages describe — is relative
indexing; `absolute_index=1` is the sounding row, on TX only. Both are gated at RTL, each with its own
csynth and its own xsim snapshot, because the parameter is a template argument and the two settings
are two designs.

What is still not implemented: **variable-length shots** (they need an allocator), **absolute indexing
on `RfShotRx`**, and **matched timing**. If one of those is what you need, that is worth knowing
before you build on this rather than after.

## Next

- [Transmit — `RfShotTx`](./tx.md) — the design as built.
- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — how the tasks share the memory, and why the transient is what it is.
- [Choosing a sample buffer](../choosing.md) — whether this family is the right one at all.
