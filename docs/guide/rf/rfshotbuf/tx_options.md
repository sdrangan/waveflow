---
title: Options and what is not built
parent: RfShotBuf
grand_parent: RF converters
nav_order: 4
audience: python
summary: "Where RfShotTx sits in the shot-buffer design space. Covers absolute indexing -- the build-time `absolute_index`, now on both halves, what it buys, what it costs on each side, where a hole is without a valid mask, and why shared addresses still need MTS -- what fixed-size relative indexing still costs you, and separately how much of the simulator's timing you are entitled to believe."
---

# Options and what is not built

`RfShotTx` sits at one point in a small design space, and **the point it sits at today is temporary**.
This page says which point, what the alternatives would buy, and what you are entitled to believe
about the simulator's timing — the last of which is a separate question about the *model* rather than
the design.

## Fixed vs. variable shot size

**Sizing and indexing are one choice, not two.** How long a shot is and what an address *means* look
independent and are not. An address can carry absolute phase — *sample j lives at `mem[j mod
BUF_LEN]`* — only if there is a fixed `BUF_LEN` to take the modulus against. Let the length vary and
the modulus has no fixed base, so the address can only mean *wherever the host put it*.

That leaves three coherent designs, not four:

| | what an address means | status |
|---|---|---|
| **fixed size, absolute indexing** | sample *j* is at `mem[j mod depth]` | **built, both halves** — `absolute_index=1` |
| **fixed size, relative indexing** | wherever it was loaded | **built**, and the default |
| **variable size, relative indexing** | wherever it was loaded | not built; needs an allocator |

### The middle row is dominated, and that is worth saying

**Fixed size buys you nothing unless you spend it on absolute indexing.** The top row spends it. The
middle row pays the constraint — your shot must be exactly `depth` words — and collects nothing a
variable-length design would not also give you. It is not a point on a frontier; it is the third row
with a restriction added.

Fixed length is not valueless, but the value is *ours*, not yours: a counted trip count is what lets
the load loop reach `II=1`, the pad needs a length to pad *to*, and an allocator stays out of the
design.

{: .note }
The one thing that could rescue the middle row is if variable length cost `II=1`, and it probably does
not: the load loop can stay counted to `depth` with the store predicated, leaving only the *play*
bound variable. Untested — `plans/rf_shot_geometry.md` claims the counted trip count for the **load**
loop, and nobody has tried the other shape.

**So the middle row is where the design is, not where it is going.** It is the right default anyway,
for a reason that has nothing to do with sizing — see below.

### Which one do you want

| you are doing | pick |
|---|---|
| channel sounding — correlate TX and RX by address, loads spaced well apart, MTS holds | `absolute_index=1` |
| play this pulse **now**, or switch waveforms faster than one pass | `absolute_index=0` |
| running a reader that is marginal on draining regions | `absolute_index=0` |

**`0` is the default even though sounding is the motivating application.** Absolute indexing is not
strictly better: a load arriving inside the deferral window is cancelled outright, so a host reloading
faster than one pass gets *nothing* while every command still answers `SHOT_LOADED`. A default that
can silently emit nothing is worse than one that puts your waveform at an address you have to ask
about — silence is the harder failure to diagnose.

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

### What it costs: one pass of latency, and a shot you may not get

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

### The receive half indexes absolutely too

`RfShotRx` takes the same `absolute_index`, and it means the same thing: its write pointer advances
on **every** block it consumes rather than only on the ones it manages to place, so a captured sample
sits at the address its own index names.

**The consequence worth knowing is what a drop does.** With relative indexing, a block the capture
had nowhere to put shifts every address after it — the samples are all valid and all in the wrong
place. With absolute indexing a drop leaves a **hole**: the samples either side of it are still
correctly placed and still usable, which is the whole point.

The cost is the mirror of the deferral TX pays. A region that is busy when its turn comes round is
skipped **entirely**, because the capture asks *is the region this index names free?* once, at that
region's first block, and holds the answer for the whole region. So a stalled reader loses more, in
whole windows rather than in blocks. That coarseness is also what buys the localisation below: an
announced window is never part stale.

### Where a hole is, without a valid mask

The header does not gain a field, and it did not need one. Every announced window is exactly one
region of words and every word the capture could not place is counted in `n_dropped`, so window *w*'s
first sample sits at absolute word index `w * region_words + n_dropped`, and the gap before it runs
from `w * region_words + n_dropped_prev` up to that. A per-block valid mask would be a second
encoding of something the header already determines — on a wire this family has deliberately held at
one 64-bit word.

`waveflow.hw.rf_shot_rx.window_abs_index` is that arithmetic, and
`RfShotRx.assert_windows_absolute` is the contract asserted against it.

### What this still does not give you

**Tile synchronisation, which is not something these designs can promise.** Both halves now index
absolutely, so TX and RX addresses are comparable *given a shared epoch* — and the epoch is the
converter's, not the buffer's. `Rfdc` models the gap: `t0_tx` is *"normally equal to `t0_rx` — that
is what MTS gives you"*, and a non-zero value means a tile deliberately started late, or a measured
MTS residual. **Absolute indexing makes the buffers able to use MTS; it cannot make MTS true.**

[`examples/rf_shot_loopback`](../../../examples/rf_shot_loopback/) is the demonstration, and it shows
both halves of that: with the two epochs tied, the address difference between the two memories **is**
the channel delay; start one tile a block late and the reading moves by exactly a block, in a way the
capture cannot distinguish from a longer path. What `t0` buys you is that the second term is zero.

**And a delay longer than one buffer is indistinguishable from `D mod depth`.** The index is a
timestamp modulo the memory, so a correlation aliases at `depth`, and the geometry has to be chosen
against the delay being measured.

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
indexing; `absolute_index=1` is the absolute row, on **both** halves. All four builds are gated at
RTL, each with its own csynth and its own xsim snapshot, because the parameter is a template argument
and the two settings are two designs.

The pair is worked through end to end in
[`examples/rf_shot_loopback`](../../../examples/rf_shot_loopback/), where the channel delay is read
off two window headers.

What is still not implemented: **variable-length shots** (they need an allocator), **matched
timing**, and **a loopback closed at RTL** — the two halves are each RTL-gated at
`absolute_index = 1`, but nothing synthesizes both into one kernel. If one of those is what you need,
that is worth knowing before you build on this rather than after.

## Next

- [Transmit — `RfShotTx`](./tx.md) — the design as built.
- [Receive — `RfShotRx`](./rx.md) — the other half of the family.
- [Internals](./tx_internal.md) — how the tasks share the memory, and why the transient is what it is.
- [Choosing a sample buffer](../choosing.md) — whether this family is the right one at all.
