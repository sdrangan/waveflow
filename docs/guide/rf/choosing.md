---
title: Choosing a sample buffer
parent: RF converters
nav_order: 9
audience: python
api: [RfShotTx, RfShotRx, RfTxStream, RfSampBufRx, Rfdc]
summary: "Which sample buffer a design should use, decided by one checkable question: does anything read the buffer while something else is writing it? A no removes the feedback channel entirely — the whole memory is payload and there is nothing to size. A yes buys unbounded duration and changeable-mid-flight data, and costs headroom, a reverse channel, and every failure mode this section's rules exist to describe. Includes the classes each family name actually resolves to, what each cannot do, and the mistake of reaching for the continuous one because it sounds more capable."
---

# Choosing a sample buffer

> **`RfShotBuf` and `RfStreamBuf` are family names, not classes.** No class of either name exists.
> They name the two *answers* to the question below, and each resolves to a concrete transmitter and
> receiver — listed under [what the names resolve to](#what-the-names-resolve-to). Reach for the
> class, import the class; the family name is for talking about the choice.

**First: you may not need one.** An `Rfdc` gives you an ordinary AXI-Stream, and consuming it
directly is the right answer for anything that processes samples as they arrive — a filter, a
detector, a decimator. See [adding an RF path](./rfdc/quickstart.md). A buffer is what you add when
you need to *hold* samples rather than pass them through.

If you do need one, there are two, and they are not a fast one and a slow one or an old one and a new
one. They differ in exactly one property, and everything else follows from it.

## The question that decides it

> **Does anything read the buffer while something else is writing it?**

That is the whole choice, and it is checkable against your own design rather than a matter of taste.

**No — nothing overlaps.** Load a waveform, *then* play it. Capture a window, *then* transfer it. Use
the **shot** family.

**Yes — they overlap.** Load the next waveform while the current one plays. Drain a capture while
still capturing. Use the **streaming** family.

## What the names resolve to

| family | transmit | receive |
|---|---|---|
| **`RfShotBuf`** (finite) | [`RfShotTx`](./rfshotbuf/tx.md) — `waveflow/hw/rf_shot_tx.py` | [`RfShotRx`](./rfshotbuf/rx.md) — `waveflow/hw/rf_shot_rx.py` |
| **`RfStreamBuf`** (continuous) | `RfTxStream` — `waveflow/hw/rf_tx_stream.py` | `RfSampBufRx` — `waveflow/hw/rf_samp_buf.py` |

**Status, and it is not uniform.** Every class in that table is built and gated at RTL, but they are
not at the same stage of their own design:

- **The shot family is complete and on one mechanism.** Both halves sit on the same
  `LockedT2pMemIF`, and both are gated through real Verilog: `RfShotTx` by
  `tests/examples/test_rf_shot_tx_xsi.py`, `RfShotRx` by `tests/examples/test_rf_shot_rx_xsi.py`.
- **The streaming family is mid-redesign.** `RfTxStream` is the finished, stream-based transmitter
  (`plans/rf_samp_new.md` Stage 1, gated by `tests/examples/test_rf_circ_play_xsi.py`).
  `RfSampBufRx` is the *older* BRAM-and-progress-channel receiver it is meant to replace; the
  stream-based receiver is Stage 2 of that plan and is **not built**. `RfSampBufRx` works and is
  gated, so it is what you use today — but read `plans/rf_samp_new.md` before building on its
  internals.

## What follows from "no"

If the writer and the reader never touch the memory at the same time, there is **nothing to
arbitrate** — and every mechanism this section spends pages on exists only to arbitrate.

No credit channel. No acknowledgements. No progress pointer. No staleness, so no margin to bound it.
The entire protocol is a command in and a verdict out.

What replaces all of it is a *lock*: one requester, one owner, and a region handed back and forth.
See [the lock](./rfshotbuf/tx_internal.md#the-lock-and-the-one-ordering-everything-turns-on) for what
that costs, which is a gap in the output at every handover and nothing else.

Two consequences worth having in mind before you assume the continuous one is the better default:

- **The whole memory is payload.** Nothing is reserved for data in transit, because nothing is in
  transit. For a long sequence that is the difference between fitting and not.
- **Pre-trigger is architecturally possible here and nowhere else.** Run a capture continuously into
  a circular buffer and stop on the trigger, and the readout includes samples from *before* the
  event — the oscilloscope model. A continuous buffer cannot do this at all: its samples flow through
  and are gone. **It is not built yet** — see [what each gives up](#what-each-gives-up).

## What follows from "yes"

Overlap buys two things nothing else can:

- **Unbounded duration.** The capture is not limited by memory, only by wherever it drains to.
- **Change the data mid-flight.** A new waveform can be loaded while the old one is still playing.

And it costs a reverse channel, because the writer must learn something the forward path cannot tell
it — either *"is there room?"* (credit) or *"what became of what I sent?"* (an ack). Which of the two
depends on the direction; see [Reverse channels](./rfdc/rules.md) for the rule that selects them.

Once that channel exists, so does everything that can go wrong with it: headroom to size, a rate
contract to satisfy, counters that must be checked rather than assumed. Those are what the
[design rules](./rfdc/rules.md) are about, and most of them are vacuous for the shot family.

## Side by side

| | shot (`RfShotTx` / `RfShotRx`) | streaming (`RfTxStream` / `RfSampBufRx`) |
|---|---|---|
| concurrency | **none** — a lock hands one region between writer and reader | writer and reader overlap |
| memory | **100% payload** | payload **plus headroom for data in flight** |
| duration | bounded by memory | **unbounded** |
| change data mid-flight | only by preempting what is playing, with a gap | **yes, seamlessly** |
| pre-trigger history | architecturally yes — **not built** | no — samples flow through |
| what can go wrong | the buffer is too small | the buffer is too small, *and* the reverse channel is mis-sized, *and* the rate contract is violated |

The last row is the honest summary. A continuous buffer has a strictly larger failure surface,
because it has strictly more mechanism.

**One row moved since this page was first written.** *Change data mid-flight* used to be a flat "no"
for the finite buffer. `RfShotTx` can now do it — a `SHOT_LOOP` playing forever accepts a load that
**preempts** it — but the handover costs a gap in the output while the memory changes hands, because
TX holds a single region. That is a real capability with a real price, not the seamless swap the
streaming transmitter gives you. See [the TX page](./rfshotbuf/tx.md#two-play-modes-and-what-a-load-does-to-each).

## Three cases

**A repeating test waveform.** Load once, replay a counted number of times or forever, change it
occasionally between runs. Nothing reads while anything writes — the reload happens between plays, or
preempts one. **`RfShotTx`**, and the memory is entirely waveform.

**A triggered capture with context.** *"Give me 100 samples around the event."* Capture into a
circular buffer, stop on the trigger, read out. **The shot family is the only family that can do
this** — the continuous buffer has already discarded the pre-trigger samples — but the capability is
**not built**: `RfShotRx` captures continuously and drops nothing, and its window is bounded by the
region rather than by the whole memory. `plans/rf_shot_buf.md` Stage C is where it is specified.

**A block delay, or anything processing a live stream.** Samples arrive forever and leave forever;
there is no point at which nothing is in flight. **The streaming family**, and the headroom is the
price of the thing you are asking for.

## The mistake to avoid

**Reaching for the streaming family because "continuous" sounds more capable.**

It is more capable in exactly two ways — unbounded duration, and seamlessly changeable in-flight
data. If your design needs neither, it gives you a smaller usable memory, a reverse channel to size,
a rate contract to satisfy, and no route to pre-trigger, in exchange for nothing.

The concurrency question is worth asking honestly rather than answered by ambition. *"We might want
to stream later"* is a reason to keep the interfaces compatible — both families speak an in-band
command on the sample stream and answer with one verdict per command — not a reason to pay for the
machinery now.

## What each gives up

**The shot family cannot run indefinitely.** When the buffer is full, acquisition stops; when the
waveform is played, it repeats, loops, or goes quiet. If a measurement is longer than memory, this is
the wrong buffer and no amount of tuning changes that.

**The shot family does not have pre-trigger *yet*.** The architecture allows it and the streaming one
forbids it, which is why the row above says *architecturally yes*. What exists is `RfShotRx`:
continuous capture with nothing dropped, but a window bounded by **one region** rather than by the
whole memory, so the history you can read back is one region deep. Full-depth pre-trigger — arm,
capture until a trigger, read back the memory ending at the trigger — is
`plans/rf_shot_buf.md` Stage C, and it is unbuilt. Do not plan around it as though it were there.

**The streaming family cannot see the past.** A command for a window that has already gone by is
refused, not served — deliberately, and reported rather than silently rounded. If you need
pre-trigger context, the continuous buffer is the wrong buffer, and that is not a limitation to work
around.

## Next

- The shot family in detail — [`RfShotBuf`](./rfshotbuf/), which has a
  [transmit page](./rfshotbuf/tx.md), a [receive page](./rfshotbuf/rx.md), and an
  [internals page](./rfshotbuf/tx_internal.md) for developers and agents.
- The design rules — [seven things that make a converter design wrong](./rfdc/rules.md), all of
  which apply to the streaming family and most of which are vacuous for the shot family.
- What the model cannot tell you — [the fidelity boundary](./rfdc/fidelity.md).
- The converter itself — [what `Rfdc` emulates](./index.md), which is the same either way.

The streaming family's own section (`rfstreambuf/`) is not written yet; it will carry the commands,
the parameters and the counters. This page is only about the choice.
