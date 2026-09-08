# Plan — the index is a timestamp

**Status: BUILT 2026-09-07, one stage.** `absolute_index` is a build-time `HwParam` on
`ShotTxPlayer` and `RfShotTx`, defaulting to `0` — today's behaviour — lowered as a fourth template
argument on `shot_tx_player_task`. Both settings are synthesized and gated at RTL. `RfShotRx` is
untouched; *What this does not deliver* is still true and is now said in the docs.

---

## What was built, and what it measured

**The three edits, in `waveflow/hw/rf_shot_tx.py` and identically in
`waveflow/build/shot_tx_player_task.h`:**

1. the advance and the wrap left `if (playing)` and became `if (ABS || playing)`; **the repeat count
   and the `done` stayed behind**, under a nested `if (playing)`;
2. accept sets `pending = arm` and leaves `rd` alone, where it used to write `rd = 0`;
3. `if (ABS) { if (pending && rd == 0) { playing = 1; pending = 0; } }`, **above** the write loop.

Plus the two the traps demanded: the `SHORT` answer is decided on `arm` rather than on `playing`, so
it is still owed immediately at accept; and the ACQUIRE branch clears `pending` as well as `playing`.

**One body, not two.** `ABS` is a template argument and the branches are `if (ABS)`; Vitis folds it.

### The numbers

| | default build | `absolute_index = 1` |
|---|---|---|
| `cmd` startup filler | 192 samples | **256** — the deferral, under the one-pass bound of 256 |
| `cmd` playout | 3 x 256 samples | **3 x 256**, unchanged — the `nrep` split held |
| `cmd_loop` playout | A, a gap, B | **nothing** — every load preempted before its boundary |
| DAC blocks zero-filled | 0 | **0** — a longer wait is longer filler, never a stall |
| `RESP_LAST_CYCLE` | 273 / 502 | 271 / 500 |
| `II` on all five loops | 1 | **1** — the `pending` bit costs nothing |

**No default-mode number moved.** Every one of `test_rf_shot_tx_xsi.py`'s 31 gates holds after a
fresh csynth of the changed body: 359 DAC words, 273 / 502, one underrun at cycle 4, zero
zero-filled, the same segment shapes, the same transients, the same port-overlap and collision
counts. That was the point of defaulting to `0`.

`WANT_XSI_GATES` **98 -> 115**: one new file, `tests/examples/test_rf_shot_tx_abs_xsi.py`, collecting
17. `ABS` is a template argument, so the mode is a second piece of RTL and needs its own `csynth` and
its own xsim snapshot; a gate that only ever elaborated the default would be asserting the mode's
behaviour against a simulator.

### The `_II_MODULES` rename, which had to be measured twice

Adding the argument renamed the player's modules in **both** builds:
`shot_tx_player_task_64_64_16_*` became `..._64_64_16_0_*` in the default build and `..._64_64_16_1_*`
in the absolute one. The first reading of the report directory said the default build's name was
*unchanged* — a plausible story about Vitis dropping a trailing zero — and it was a reading of RTL
synthesized before the parameter existed. `rtl_staleness` refused the stale project, the II gate
**skipped**, and the session gate failed on the skip. That is the sequence the whole no-silent-skip
apparatus exists for, working.

## Assumptions taken during the build

These were not in the plan; each is what its reasoning implies.

**`cmd_loop` under `absolute_index = 1` plays nothing, and that is gated rather than worked around.**
The plan's cost line — *"latency, bounded by one pass"* — bites on that stream: its loads are spaced
closer than one pass, so each is preempted while still armed. Rather than invent a wider-spaced
scenario, the run is asserted as it is. It is the honest statement of what the mode costs, and it is
also the only place a stale arm would be visible: an ACQUIRE that cleared `playing` without clearing
`pending` would start the outgoing shot at the next boundary out of a memory the incoming load has
already rewritten, and the capture is supposed to be empty.

**The negative control runs in pysim, not at RTL.** What it must isolate is the *parameter*, and the
pysim pair differ in nothing else; the RTL pair differ in a snapshot as well, so a failure there would
have a second candidate explanation. The positive half of the same pairing *is* asserted at RTL.
There is a second, independent negative control at the toolchain-free tier
(`tests/hw/test_rf_shot_tx.py`), and a **positive** control for the `nrep` trap: the wrong edit,
shipped as a class, which plays two passes where three were asked for while answering every header
normally.

**The second build is a subclass, `RfShotTxAbs`, defined in the example.** It sets
`cpp_kernel_name` and the parameter's default and overrides nothing else. It exists for the *name*: a
Vitis project, an xsim snapshot and a generated ports header are all keyed on the kernel's name, and
the two variants have to sit in one example directory to be compared against each other. The
testbench graph is the same `RfShotTxTB`, cut at a different DUT class — a second graph would be a
second model of one design.

**`assert_finite_completed` also refuses a run that ends with a shot still armed.** A boundary that
never arrives is a player that stopped counting, and it would otherwise look like a shot that was
never loaded.

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

## What changed — three edits, and they were small

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

## Gates — all built

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
