---
title: Choosing a sample buffer
parent: RF converters
nav_order: 9
audience: python
api: [RfShotBuf, RfStreamBuf, Rfdc]
summary: "Which sample buffer a design should use, decided by one checkable question: does anything read the buffer while something else is writing it? A no removes the feedback channel entirely — the whole memory is payload and there is nothing to size. A yes buys unbounded duration and changeable-mid-flight data, and costs headroom, a reverse channel, and every failure mode this section's rules exist to describe. Includes what each buffer cannot do, and the mistake of reaching for the continuous one because it sounds more capable."
---

# Choosing a sample buffer

> **Status, stated first.** `RfStreamBuf` is built and RTL-gated. **`RfShotBuf` is designed, not
> built** — this page is written ahead of it so the choice is stated before either is reached for,
> and so the design has something to be checked against. Where a claim here rests on a measurement,
> it says so; where it rests on the design, it says that too.

**First: you may not need one.** An `Rfdc` gives you an ordinary AXI-Stream, and consuming it
directly is the right answer for anything that processes samples as they arrive — a filter, a
detector, a decimator. See [adding an RF path](./python/quickstart.md). A buffer is what you add when
you need to *hold* samples rather than pass them through.

If you do need one, there are two, and they are not a fast one and a slow one or an old one and a new
one. They differ in exactly one property, and everything else follows from it.

## The question that decides it

> **Does anything read the buffer while something else is writing it?**

That is the whole choice, and it is checkable against your own design rather than a matter of taste.

**No — nothing overlaps.** Load a waveform, *then* play it. Capture a window, *then* transfer it. Use
**`RfShotBuf`**.

**Yes — they overlap.** Load the next waveform while the current one plays. Drain a capture while
still capturing. Use **`RfStreamBuf`**.

## What follows from "no"

If the writer and the reader never touch the memory at the same time, there is **nothing to
arbitrate** — and every mechanism this section spends pages on exists only to arbitrate.

No credit channel. No acknowledgements. No progress pointer. No staleness, so no margin to bound it.
The entire protocol is *go* and *done*.

Two consequences worth having in mind before you assume the continuous one is the better default:

- **The whole memory is payload.** Nothing is reserved for data in transit, because nothing is in
  transit. For a long sequence that is the difference between fitting and not.
- **Pre-trigger comes free.** Run the capture continuously into a circular buffer and stop on the
  trigger, and the readout includes samples from *before* the event — the oscilloscope model. The
  continuous buffer cannot do this at all: its samples flow through and are gone.

## What follows from "yes"

Overlap buys two things nothing else can:

- **Unbounded duration.** The capture is not limited by memory, only by wherever it drains to.
- **Change the data mid-flight.** A new waveform can be loaded while the old one is still playing.

And it costs a reverse channel, because the writer must learn something the forward path cannot tell
it — either *"is there room?"* (credit) or *"what became of what I sent?"* (an ack). Which of the two
depends on the direction; see [Reverse channels](./python/rules.md) for the rule that selects them.

Once that channel exists, so does everything that can go wrong with it: headroom to size, a rate
contract to satisfy, counters that must be checked rather than assumed. Those are what the
[design rules](./python/rules.md) are about, and none of them apply to `RfShotBuf`.

## Side by side

| | `RfShotBuf` (finite) | `RfStreamBuf` (continuous) |
|---|---|---|
| concurrency | **none** — so no feedback channel exists | writer and reader overlap |
| memory | **100% payload** | payload **plus headroom for data in flight** |
| duration | bounded by memory | **unbounded** |
| pre-trigger history | **yes** — stop on trigger, read the past | no — samples flow through |
| change data mid-flight | no | **yes** |
| what can go wrong | the buffer is too small | the buffer is too small, *and* the reverse channel is mis-sized, *and* the rate contract is violated |

The last row is the honest summary. A continuous buffer has a strictly larger failure surface,
because it has strictly more mechanism.

## Three cases

**A repeating test waveform.** Load once, replay forever, change it occasionally between runs.
Nothing reads while anything writes — the reload happens between plays. **`RfShotBuf`**, and the
memory is entirely waveform.

**A triggered capture with context.** *"Give me 100 samples around the event."* Capture into a
circular buffer, stop on the trigger, read out. **`RfShotBuf`** — and it is the *only* option, since
the continuous buffer has already discarded the pre-trigger samples.

**A block delay, or anything processing a live stream.** Samples arrive forever and leave forever;
there is no point at which nothing is in flight. **`RfStreamBuf`**, and the headroom is the price of
the thing you are asking for.

## The mistake to avoid

**Reaching for `RfStreamBuf` because "continuous" sounds more capable.**

It is more capable in exactly two ways — unbounded duration, and changeable-in-flight data. If your
design needs neither, the continuous buffer gives you a smaller usable memory, a reverse channel to
size, a rate contract to satisfy, and no pre-trigger, in exchange for nothing.

The concurrency question is worth asking honestly rather than answered by ambition. *"We might want
to stream later"* is a reason to keep the interfaces compatible — the command and response types are
shared — not a reason to pay for the machinery now.

## What each gives up

**`RfShotBuf` cannot run indefinitely.** When the buffer is full, acquisition stops; when the
waveform is played, it repeats or ends. If a measurement is longer than memory, this is the wrong
buffer and no amount of tuning changes that.

**`RfStreamBuf` cannot see the past.** A command for a window that has already gone by is refused,
not served — deliberately, and reported rather than silently rounded. If you need pre-trigger
context, the continuous buffer is the wrong buffer, and that is not a limitation to work around.

## Next

- The design rules — [seven things that make a converter design wrong](./python/rules.md), all of
  which apply to `RfStreamBuf` and most of which are vacuous for `RfShotBuf`.
- What the model cannot tell you — [the fidelity boundary](./python/fidelity.md).
- The converter itself — [what `Rfdc` emulates](./index.md), which is the same either way.

The per-buffer pages (`rfshotbuf/`, `rfstreambuf/`) are not written yet; they will carry the
commands, the parameters and the counters for each. This page is only about the choice.
