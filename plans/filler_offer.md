# Plan — filler is presented, not written

**Status: SCOPED HERE, NOTHING BUILT.** Started 2026-09-06. Owns the question S3 left open: whether a
free-running player can be stopped from running ahead of its own data. Downstream of
`plans/pysim_burst_backpressure.md`, whose S3 measured the problem and refused to guess at the fix.

---

## Why

`plans/pysim_burst_backpressure.md` S3 measured, and the result is the premise here:

> **Back-pressure paces a producer's RATE. It does not bound how far AHEAD of its data it may get.**

With `ShotTxPlayer.dac_word_rate` neutralised, throughput and correctness were untouched — same
underrun, blocks, passes, grants — but the lead filler went **192 → 640 samples**, and pysim stopped
agreeing with RTL. The mechanism is simple and general: **a `FreeRunMod` player always has something
to write.** The instant the downstream has room it fills it with filler, and the real shot queues
behind words nobody asked for.

No queue depth expresses this — sweeping `dac_axis` 32/16/8/4/2 gives 640/576/576/576/576, and the
disagreement survives at the RTL's own depth of 2.

So the metronome cannot be retired by pacing alone. Either the rate stays declared, or the player
stops being able to run ahead. This plan is about the second.

## The idea

`interface.py:868-880` already makes the distinction this needs, and makes it well:

> the difference is a property of the **PRODUCER**, not of the wire … an ordinary module keeps
> calling `write()` while a converter calls `offer()`.

A player emitting **filler** is, for those words, behaving exactly like a converter: it presents a
beat whether or not anyone wants it, and what is not taken is not worth keeping. A player emitting
**real samples** is a module: those words are the data, and losing one is a defect.

So the proposal is a producer that uses **both disciplines on the same port, chosen per word by what
the word means**:

| the word is | discipline | if there is no room |
|---|---|---|
| a real sample from the shot | `write()` | **stall** — this word matters |
| filler | `offer()` | **drop it** — the next one is just as good |

A player that cannot enqueue filler when the queue is full cannot run ahead, because running ahead is
*exactly* the act of filling the queue with filler. The lead collapses to what the data itself
justifies, and the metronome has nothing left to do.

## The tension that decides this, and it is not small

**The design's stated principle is that quiet must be PRODUCED, not invented.**
`test_the_dac_is_never_starved_on_either_path` asserts `DAC_BLOCKS_ZERO_FILLED == 0`, and says why:

> Quiet is supposed to be silence the DESIGN produces, not silence the grid invents.

Dropped filler is silence the grid invents. So at first reading this plan proposes exactly what that
gate forbids.

**And there is a twin-divergence risk underneath it.** `offer()` is a pysim concept; at RTL the player
writes and `TREADY` paces it. A player that *drops* filler in pysim where the RTL *stalls* is two
different designs — the divergence this repo treats most seriously, and the one
`plans/rf_shot_buf.md` already paid for once.

**Neither objection is fatal, and neither is obviously survivable.** The case for proceeding:

* At RTL the player never gets ahead in the first place, because `TREADY` is the queue being full.
  So dropping filler in pysim may be modelling the *same* outcome by a different mechanism, not a
  different outcome.
* A dropped filler word and an arrived filler word may be indistinguishable **at the converter**,
  since the grid's zero-fill and the design's filler can carry the same value.

**Both of those are claims, not facts, and S1 exists to settle them before anything is built.** If
they do not hold, this plan is refused and the metronome stays — which, after S3, is a perfectly good
outcome.

## Stages

### S1 — settle the two claims, build nothing

Answer, with measurements:

1. **Is a dropped filler word observably different from an arrived one, at the converter?** Compare
   what the `Rfdc` model does with a zero-filled block against what it does with a design-produced
   filler block. If they differ, `DAC_BLOCKS_ZERO_FILLED` is measuring something real and this plan
   is much harder.
2. **Does the RTL player ever get ahead?** If `TREADY` genuinely prevents it, then pysim-drop and
   RTL-stall reach the same place and the divergence is in mechanism only. If the RTL *does* run
   ahead by some bounded amount, that amount is the thing pysim should reproduce — and neither
   `write()` nor `offer()` alone gives it.

**Do not touch a player in S1.** As with the back-pressure arc, the measurement is the deliverable and
it may refuse the plan.

### S2 — the per-word discipline, if S1 permits

`offer()` for filler, `write()` for data, on the same port. The player's loop stops needing a
deadline.

**Gate:** the lead filler must match RTL's — 3 blocks on the gated `rf_shot_tx` configuration — with
the metronome removed. That is the number S3 measured going wrong (192 → 640), so it is the number
that proves this worked.

`test_both_backends_agree_sample_for_sample` must hold, and `DAC_BLOCKS_ZERO_FILLED` must be
whatever S1 established it should be — restated deliberately if S1 changed what it means.

### S3 — then, and only then, retire the metronome

`ShotTxPlayer.dac_word_rate` goes, and `blk_words` becomes only the lock poll period. This is
`plans/pysim_burst_backpressure.md` S3 retried with the obstacle removed rather than assumed away.

The other two stay regardless and for their own reasons, both recorded there:
`RfSampBufPlayer.dac_word_rate` is `max(fabric, demand)` and models which side is the bottleneck;
`RfTxStream.slot_period` raises when unset and is a guard, not pacing.

## Traps

**The gate that cannot see this.** `test_the_dac_is_never_starved_on_either_path` reads **XSI
counters**, so it is structurally blind to any pysim-only change and stays green through a bad one.
The gates that can see it are the pysim ones: the example's own underrun/segments, and
`test_both_backends_agree_sample_for_sample`. This cost the back-pressure arc a wrong instruction;
do not repeat it.

**`offer()` already has a drop counter.** `StreamIFMaster.dropped` exists and is the right place for
filler that did not fit. A silent drop here would be the worst version of this change.

**Do not make it an interface flag.** `interface.py:868-880` argues at length that blocking-versus-not
is a property of the producer and that putting it on the edge is a category error *"already caught
twice on this arc"*. Per-word choice by the producer is consistent with that; a `StreamIF(drop=True)`
is not.

## Not in scope

- `RfSampBufPlayer` and `RfTxStream`. They keep their pacing for reasons that have nothing to do with
  filler.
- The word-granular `data_buffer` question from `plans/pysim_burst_backpressure.md` — the 6 internal
  channels under-counting their stalls. Separate, larger, and not blocking this.
- Deriving the rate from the converter's clock. That is the *other* follow-up S3 recorded, it is
  independent of this one, and it is worth doing whether or not this plan survives S1.
