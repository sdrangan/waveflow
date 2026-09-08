# Plan — the index is a timestamp

**Status: SCOPED 2026-09-07, NOTHING BUILT.** Adds absolute indexing to `RfShotTx` as a build-time
mode, defaulting to today's behaviour. Does not touch `RfShotRx` — see *What this does not deliver*,
because that section is the reason this plan is only half a feature.

---

## Next session starts here — S1

```
claude "Read plans/rf_shot_absolute.md and build it.  One stage.  `absolute_index` defaults
        to 0 so every existing gate keeps its meaning; the new mode needs its OWN gates and
        its own RTL.  The nrep trap in 'Traps' is the one that fails silently."
```

---

## Why

`docs/guide/rf/rfshotbuf/tx_options.md` names three coherent designs and says today sits at the
**intersection** of the other two's constraints: fixed size, which the sounding design needs, with
relative indexing, which the general design settles for. A user accepts that a shot must be exactly
`depth` words and gets nothing back for it.

Absolute indexing is what fixed size was supposed to buy. **Memory index becomes a timestamp**: if
transmitted sample *j* sits at `mem[j mod depth]` and a received sample *j* likewise, TX and RX
correlate **by address**, with no timestamping and no bookkeeping. For channel sounding that
correlation *is* the measurement.

## What changes — three edits, and they are small

The design already contains the counter. `play_chunk` writes `BW` words on **every** firing —
`samp_out.write(playing ? buf[rd + i] : SHOT_TX_FILLER)` — so the number of words it has emitted
since reset *is* the absolute word index, and `#pragma HLS reset variable=rd` anchors it at the
epoch. Three edits turn `rd` into that index:

| | today | with `absolute_index` |
|---|---|---|
| advance | `if (playing) { rd += BW; ... }` | `rd += BW` **unconditionally**, wrap unconditionally |
| on accept | `rd = 0` — *"a new waveform starts at its beginning"* | **do not touch `rd`**; set `pending` |
| start | `playing = (nrepeat != 0)` on accept | `playing = 1` when `pending && rd == 0` |

Python sites: `waveflow/hw/rf_shot_tx.py` — the accept reset, the guarded advance, and the filler
branch. C++ twin: `waveflow/build/shot_tx_player_task.h` — the same three, in the same order.

## The boundary rule, and why this is not a semantic tradeoff

The obvious cost of absolute indexing is that playout begins wherever the counter happens to be, so
**the first sample out is not the first sample of your waveform**. That is right for sounding and
wrong for *play this pulse now*, and it is what would make this a mode rather than an improvement.

**Deferring the start to a buffer boundary removes the tradeoff instead of trading it.** The
constructor already requires `blk_words` to divide `depth`, so `rd` takes exactly the values
`0, BW, 2BW, ... D-BW` and `rd == 0` occurs **exactly once per pass** — an exact test, with no
crossed-zero case to get wrong. Because the waveform fills the whole buffer *and* starts on a
boundary, sample *j* is then always emitted at an absolute index congruent to *j*: the full timestamp
property, with *a waveform starts at its beginning* kept intact.

The cost is latency, bounded by one pass — at the gated geometry `depth / blk_words = 4`, so at most
three chunks of extra filler after a load lands.

**It also survives the loosely-timed model, which is the part that was not obvious.** The player only
ever writes whole chunks, so the LT lead is structurally a whole number of chunks. Both backends
therefore start on a boundary of their **own** counter: they disagree about which **pass** a shot
lands in and agree exactly about **phase within the pass**. Phase is what a sounding correlation
uses, so the property this plan adds is not one the LT relaxation takes away.

## The parameter

`absolute_index: HwParam[int] = 0` on `ShotTxPlayer` and on the `RfShotTx` composite, lowered as a
template argument. **Zero is today's behaviour**, so every existing gate keeps its meaning and any
number that moves is a finding rather than an expected consequence.

It is genuinely build-time: it changes the RTL, so it is an `HwParam` and not a runtime flag. The
timing-fidelity mode (`tx_options.md`, *Loosely timed vs. matched timing*) is **not** — the RTL is
identical either way and it belongs on the simulation side. Do not add it here.

## Gates

**The address is the phase.** For every played word, its address equals the absolute word index
modulo `depth`. This is the whole feature, asserted directly.

**A playout starts on a boundary.** Every segment begins at an absolute index that is a multiple of
`depth`. One assertion, and the one a reader can check by eye against a log.

**The negative control: at `absolute_index = 0` the same assertion fails.** Without it the parameter
could do nothing and every gate would still pass. `plans/lt_transient.md` shipped three gates whose
negative controls were never committed; do not repeat that.

**Load time moves the pass, not the phase.** Run the same shot with the load arriving at two
different times and assert the phase relation holds in both while the starting pass differs. This is
the experiment the merged TX/RX example wants, and it is checkable with TX alone.

**Both settings reach RTL.** Two variants, so `csynth` runs twice and `WANT_XSI_GATES` grows. Say by
how much and why rather than letting the count drift.

## Traps

**THE `nrep` DECREMENT MUST STAY GATED ON `playing`.** Today the wrap and the repeat count live in
one block:

```c
if (playing) { rd += BW; if (rd >= D) { rd = 0; if (!loop) { if (--nrep_left == 0) ... } } }
```

Moving that block out of `if (playing)` is the natural edit and it is **wrong**: the count would
decrement once per pass while the design is playing filler, and a finite shot would end early or
never start. Split it — advance and wrap unconditionally, `nrep_left` and `done` still gated on
`playing`. It fails silently: the word counts still add up.

**The SHORT path must not defer.** `if (!playing && !loop) done_out.write(1)` answers a shot that
must never play. The loader is blocked on that token, so routing it through `pending` deadlocks
rather than failing a gate. A short shot is owed its `done` immediately, at accept.

**The ACQUIRE path must clear `pending`, not just `playing`.** In practice every RELEASE carries a
fresh play command that overwrites it, but a stale arm surviving a lock handover is the kind of
defect that works until it does not.

**`SHOT_BUSY` widens.** `busy` clears on `done`, so deferring a start holds it up to one pass longer.
Cycle counts move predictably — re-measure, do not re-record.

**Preemption is unchanged, and do not claim otherwise.** An ACQUIRE still sets `playing = 0` before
granting, so the outgoing waveform is still cut mid-pass. Deferral governs only when the *incoming*
one starts.

**II must be re-measured.** The state test is trivial and the wrap moves *out* of a branch, which is
simpler rather than harder — but `test_every_pipelined_loop_reaches_ii_1` is the witness, and its
`_II_MODULES` names carry the template arguments, so a new parameter **renames every module**. Read
the names off the report directory; a stale one makes that gate skip, which reads as a pass.

## What this does not deliver

**The sounding use case needs both ends, and this is the TX half.** `RfShotRx` captures continuously
into two regions and announces each with a `base_addr` on the wire; whether its addresses can carry
absolute phase is a separate question with a different answer, because a capture that drops a block
loses its place in a way a player cannot. Correlating TX against RX by address is not available at
the end of this plan.

Say that in the docs when it lands. A page claiming *"index becomes a timestamp"* while only one end
indexes absolutely would be worse than today's, which at least admits the feature is absent.

## Not in scope

- **`RfShotRx`.** Above.
- **Matched timing.** `plans/lt_transient.md` S3. Independent of this, and not an `HwParam`.
- **Variable-length shots.** The other row of `tx_options.md`, and it needs an allocator —
  `plans/t2p_lock_chan.md` S3 fenced that off as *"where this stops being an interface and starts
  being arbitration."*
- **MTS itself.** `t0_tx ≡ t0_rx` is a property of the converter and the board. This plan makes the
  buffer able to use it; it cannot make it true.
