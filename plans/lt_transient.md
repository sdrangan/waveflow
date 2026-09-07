# Plan — loosely-timed by default, matched on demand

**Status: S1 MEASURED and S2 BUILT, 2026-09-07. Only S3 (Matched mode) is left, and it is
optional.** Owns what the pysim↔RTL comparison asserts about *timing*, and the metronome parameters
that existed to satisfy the old answer. **Replaces `plans/filler_offer.md`**, which proposed a
player-side fix for something the player was not causing.

---

## Next session starts here — S3, **and only if wanted**

**S1 and S2 are DONE** (2026-09-07, branches `lt-transient-s1` / `lt-transient-s2`) — see *S1 as
measured* and *S2 as built*. The three gates are in, `ShotTxPlayer.dac_word_rate` is retired, and no
number the design produces moved: the generated C++ is byte-identical and every RTL measurement holds.

S3 is **Matched mode** — a debugging tool, not a gate. Nothing depends on it.

```
claude "Read plans/lt_transient.md, sections 'Matched mode, later' and 'S2 as built', and build S3.
        It is a DEBUGGING mode: it must not become the reference, and it must not be what any gate
        runs in."
```

---

## Why this exists

It began as a question about a constructor argument: *why does `RfShotTx` take a `dac_word_rate` when
the hardware has no such thing?* Following it down took four days and three wrong turns, and the
answer is worth stating plainly because it is not what anyone expected:

**`dac_word_rate` is the price of a gate specification nobody ever chose.**

`test_both_backends_agree_sample_for_sample` asserts

```python
n = min(int(rtl.size), int(py.size))
assert np.array_equal(rtl[:n], py[:n])
```

— **byte-identical from t=0, including the startup transient**. Nothing decided that; it was written
as `array_equal` because that is the obvious thing to write. And the pysim model does not reproduce
the transient naturally: the metronome is what forces it onto the same time grid so the obvious
assertion can pass.

So the parameter is not a wart. It is a tax, on a fidelity level that **loosely-timed modelling does
not promise and `RfShotTx`'s use cases do not need**.

### What was tried first, and why it failed

Recorded so it is not retried:

* **`plans/pysim_burst_backpressure.md` S1/S2** — a pysim burst write was back-pressured for exactly
  one word and then absorbed into an unbounded counter. Real defect, fixed, no gate number moved. It
  did **not** retire the metronome.
* **S3** — removed the metronome and measured the result. **Refuted**: back-pressure paces *rate*,
  not *lead*. Throughput was untouched — same underrun, blocks, passes, grants — while the lead
  filler went 192 → 640 samples and the backends stopped agreeing.
* **`filler_offer.md`** — proposed `offer()` for filler so the player could not run ahead. Aimed at
  the player, which was never the problem.

The player's pysim twin is a faithful model of its HLS: it writes, and it blocks. What is not
faithful is the *converter*, whose DAC path reacts to word arrivals **from the fabric** — the unpaced
side — where the ADC path correctly reacts to block arrivals from RF.

## The decision

**Two modes, and the default is the honest one.**

| | pacing | transient | used for |
|---|---|---|---|
| **LT** (default, built now) | back-pressure only | **bounded, not matched** | every gate |
| **Matched** (later) | `Rfdc` emits a timing event | reproduced exactly | debugging a value mismatch |

**Gate in LT. Debug in Matched.** Matched mode makes pysim run a model the RTL does not have — that is
its job — but a defect visible only there may not exist in the mode that ships. Its purpose is to take
the transient out of the way so a *value* difference is legible, never to be the reference.

**Both modes retire `dac_word_rate`.** Matched mode's signal comes from the `Rfdc`, which already holds
`tx_samp_rate` read from the interface's clock. One derived event from the component that has a clock
replaces three declared rates on components that do not.

## What the gates become

### 1. Phase — per backend, per segment

The strongest of the three, and it is **not** a cross-backend comparison. Each backend is checked
against the *waveform*:

```python
assert real[i] == shot_codes[i % nsamp]      # within one playout segment
```

It catches read-pointer errors, wrap errors and off-by-ones at the buffer boundary, and it **cannot be
broken by an LT transient** — it does not care when playing started. It is also more diagnostic than
what it replaces: `array_equal` against the other backend says they differ; this says *which one has
the wrong phase*.

**Per segment, not per run.** `RfShotTx` restarts the shot from its beginning after a preemption, so a
whole-run assertion would fail for the right reason and look like a bug.

### 2. Agreement — aligned on a logged event

Each stream is segmented at **its own** logged events, and each segment compared **exactly** after
`guard` samples. Startup and handover are the same case: a handover is just another logged event.

```python
def compare_after_transients(a, log_a, b, log_b, *, guard):
    assert [e for e, _ in log_a] == [e for e, _ in log_b], "the runs saw different events"
    for k, (ev, ia) in enumerate(log_a):
        ...  # compare a[ia+guard:...] against b[ib+guard:...], exactly
```

**Exact, not MSE.** These are integer converter codes computed from the same golden on both sides. Any
difference is a defect, and a tolerance would hide a single wrong sample.

The event-sequence assertion is load-bearing: two runs that saw *different events* is a real
divergence and must fail loudly rather than be papered over by alignment.

### 3. The transient — recorded, not cross-compared

Each backend reports its own lead as a pinned number. A **change** in either is still a finding; a
**difference between them** stops being a failure. The risk in relaxing a gate is silently ceasing to
measure, and this is what prevents it.

## The log, and the one thing it depends on

Both sides derive their own log from their own output: `segments()` already splits a playout into
`(is_filler, samples)` runs, and the filler→play transitions **are** the log. No VCD, no
cycle-to-sample mapping.

**It works only because the golden excludes `FILLER`.** `FILLER = 0` and the shot is a ramp from
`CODE_BASE = 1000`, so a real sample is never 0 — `segments()`' docstring says this was chosen
deliberately. **A realistic waveform crosses zero constantly**, and then "idle" and "transmitting a
zero" are the same bytes.

So make the convention an assertion:

```python
assert shot_codes(...)[0] != FILLER, "the golden must start non-zero or the log start is ambiguous"
```

**Stronger than "has at least one non-zero sample."** A waveform opening with 50 zeros would put the
boundary 50 samples late in *both* backends — identically, so the alignment survives — but the guard
would then be measured from the wrong origin and the first 50 samples of every playout would go
silently uncompared. Alignment is robust to that error; **coverage is not.**

**When the log stops being derivable**, cross-correlating the known golden against the played stream
finds the alignment regardless of the values. That is the general mechanism; the log is the
optimisation available when the data can self-segment.

## The guard, which must be derived and not chosen

Every stage between the player and the paced consumer blocks on a **finite declared depth**: the AXIS
queue (`2 × blk_words`), `_dac_proc`'s in-flight block, `tx_rf._buf` (`DEFAULT_RF_DEPTH`), and the
sink queue (`DEFAULT_RF_RX_DEPTH`). **So the transient is bounded** — and it is bounded *because* of
`pysim_burst_backpressure` S2, which removed the unbounded `ntx` path. Before that there was no bound
at all.

So do not pick `guard`. **Derive it from those depths, and assert the measured transient sits under
it.**

Two reasons, and the second is the important one:

1. It tracks automatically when `blk_words` or a queue depth changes.
2. **It catches a stage nobody modelled.** Adding those depths gives ~448 samples against the **576**
   S3 measured — a **128-sample gap that is not accounted for**. A derived-bound assertion fails on
   exactly that and names the problem; a hand-picked `guard` would have absorbed it silently.

That gap is why S1 measures before anything is asserted.

> **Corrected by *S1 as measured*, below. The reasoning above is kept because it did its job — the
> derived-bound discipline is what found the two errors — but four of its numbers are wrong.** The
> four-stage list is two stages short (the composite's own `samp` FIFO and the re-layout task) and
> one stage long (the sink queue holds nothing, because the sink drains without waiting); the
> gated-geometry S3 measurement is **640**, not 576 (576 is a row of S3's depth *sweep*); and the
> depth sum bounds the **lead**, not the transient — the transient also carries the design's own load
> latency, which no depth expresses. There is no unaccounted 128: `-128 + 128 = 0`.

## Matched mode, later

The `Rfdc` emits a timing event on the transient so the two backends coincide. Not a timer of its own
— **the docstring's "reactive, no timer" principle is upheld**: a request is reactive, and the rate it
would use is `tx_samp_rate`, already read from the interface clock.

Worth having, as a debugging tool: when a value comparison fails, being able to remove the transient
difference is what makes the remaining difference legible.

## Option 1 — fixed `BUF_LEN` — a different contract, recorded not scheduled

The design today places a shot of `nword` words at `base` and plays from the region's start each pass
(**Option 2**). The alternative:

**Option 1** — loader puts sample *i* at `mem[i]`; the player at sample *j* plays `mem[j % BUF_LEN]`;
RX likewise. Then **memory index is a timestamp**, and TX and RX samples correlate *by index* with no
timestamping — which for channel sounding is the measurement itself.

| | Option 2 (built) | Option 1 (future) |
|---|---|---|
| index means | offset within *this shot* | **absolute sample counter mod `BUF_LEN`** |
| depends on | nothing outside the design | **`t0_tx ≡ t0_rx`** — MTS actually holding |
| buys | flexible shot length, multiple regions | TX↔RX correlation by index |

`Rfdc` already models the dependency: `t0_tx` is *"normally equal to `t0_rx` — that is what MTS gives
you"*, with a non-zero value meaning a tile started late or a measured MTS residual. **So Option 1's
guarantee degrades exactly as tile sync does**, and the model can already express that.

**Its gate would be different, not stricter**: absolute (`sample_j == mem[j % BUF_LEN]` against a
global counter) rather than per-segment. Do not build it by tightening this plan's phase check.

## What retires — **done, see *S2 as built***

* `ShotTxPlayer.dac_word_rate`, and the hand-computed `samp_rate / samp_per_word` in the example.
* `blk_words`'s second meaning — it becomes only the lock poll period.
* The other two metronomes stay for their own measured reasons, recorded in
  `plans/pysim_burst_backpressure.md`: `RfSampBufPlayer.dac_word_rate` is `max(fabric, demand)` and
  models which side is the bottleneck; `RfTxStream.slot_period` raises when unset and is a guard.

## Traps

**Relaxing a gate must not stop a measurement.** Gate 3 exists for this: the transient stays pinned
per backend even though it is no longer cross-compared.

**The stale docstring.** ~~`rf_shot_tx.py:689-690` still says *"pysim does not back-pressure a burst
write"*.~~ **Fixed in S2**, along with `_chunk_and_pace`, whose name became a lie once it stopped
pacing.

**Cycle counts are measurements.** Relaxing the comparison changes what is asserted, not what the
design does — so a *design* number that moves is a finding, not a re-record.

## S1 as measured — **the bound holds, and both halves of the 128-sample gap were errors**

Measured 2026-09-07, branch `lt-transient-s1`, gated geometry
(`blksize = 64`, `samp_per_word = 4`, `blk_words = 16`, `nword = 64`, `n_blk = 20`).
Nothing under `waveflow/` or `examples/` was edited: the depths were read off the bound interface
graph, and the occupancies off read-only wrappers around the endpoint methods in the measuring
process — the same technique `plans/pysim_burst_backpressure.md` S1 used.

### 1. The derived bound

The quantity the declared depths bound is **the lead** — how far ahead of the converter's grid the
free-running player may get — and **not** the transient. In samples:

```
C_lead = SPW * ( max(D_samp, B)             # 1  the composite's own `samp` FIFO
               + B                          # 2  the re-layout task's in-flight burst
               + max(D_dac,  B)             # 3  the testbench's `dac` FIFO
               + B )                        # 4  the Rfdc's DAC process, in-flight block
       + BLK * D_rf                         # 5  RFSampIF's producer-side buffer
       + BLK * D_rx * [consumer can wait]   # 6  the sink's own queue -- ZERO here, see below
```

| # | term | value | where the number comes from |
|---|---|---|---|
| — | `SPW` | 4 | `Rfsoc4x2SampWord.specialize(samp_per_word=4)` — `examples/rf_shot_tx/rf_shot_tx.py`, `WORD` |
| — | `B` = `blk_words` | 16 words | `blksize // SPW`, `examples/rf_shot_tx/rf_shot_tx.py:284`; the same one number reaches `ShotTxPlayer.blk_words` and `RfRelayoutToSlots.blk_words` |
| — | `BLK` = `blksize` | 64 samples | `RfShotTxTB.blksize` |
| 1 | `D_samp` | 2 words | `RfShotTx.__post_init__`, `waveflow/hw/rf_shot_tx.py:934` — the `("samp", …, 2)` row |
| 2 | — | `B` | `_RelayoutTask.run_iter`, `waveflow/hw/rf_relayout.py:225` — one `get(nwords_max=blk_words)` held across the `write` |
| 3 | `D_dac` | `2 * blk_words` = 32 words | `examples/rf_shot_tx/rf_shot_tx.py:319` — the testbench's `dac` row |
| 4 | — | `tx_blksize // SPW` = `B` | `Rfdc._dac_proc`, `waveflow/hw/rfdc.py:579` — one block held across `tx_rf.put` |
| 5 | `D_rf` | 2 blocks | `RFSampIF.depth` = `DEFAULT_RF_DEPTH`, `waveflow/hw/rf_sample_if.py:59` |
| 6 | `D_rx` | 2 blocks | `RfDataSink.depth` = `DEFAULT_RF_RX_DEPTH`, `waveflow/hw/rf_sample_if.py:62` |

**`max(D, B)` is not a flourish, and the `samp` FIFO is where it bites.** `_admit_blocking`
(`waveflow/hw/interface.py:627`) has two regimes: a burst that *fits* waits for room, and the
channel's capacity is its depth; a burst **larger than the whole queue** waits for the queue to be
empty and then goes in whole — one burst in flight. `D_samp` is 2 and the burst is 16, so that
channel's capacity is **16 words, not 2**. A formula that read `depth` off the interface and stopped
there would understate this term eightfold.

**Term 6 is zero, and that is a property of the consumer rather than of the depth.**
`RfDataSink.run_proc` (`waveflow/simulation/rf_tb.py:325`) appends and loops with nothing to wait
for, so its queue is drained in the same event it is filled. Measured occupancy at the switch: **0
of 2 blocks**, and sweeping `D_rx` over 1, 2, 4, 8 does not move the transient by one sample. It
stays in the formula with its predicate because a consumer that *can* wait would occupy it.

So at the gated geometry:

```
C_lead = 4*(16 + 16 + 32 + 16) + 64*2 + 0 = 320 + 128 = 448 samples
```

### 2. The measured transients

`segments()` on the played stream: the leading filler run is the startup transient, each interior
filler run is a handover. **pysim LT** is the gated graph with `dac_word_rate` neutralised on the
player and nothing else changed.

| | startup | handover | trailing quiet |
|---|---|---|---|
| **`cmd`** RTL (XSI) | **192** | — (none: one continuous run) | 448 |
| **`cmd`** pysim, paced | **192** | — | 320 |
| **`cmd`** pysim, **LT** | **640** | — | 0 — *the run ends mid-playout* |
| **`cmd_loop`** RTL (XSI) | **192** | **128** | 960 |
| **`cmd_loop`** pysim, paced | **192** | **128** | 832 |
| **`cmd_loop`** pysim, **LT** | **640** | **128** | 384 |

All figures in samples; 64 samples is one converter block. The RTL and paced-pysim trailing runs
differ only because the two runs stop at different horizons (1400 cycles = 22 blocks against
`n_blk = 20`) — which is exactly what `test_both_backends_agree_sample_for_sample` already compares
over a common prefix for.

Three things this table says:

* **The startup transient is the same in both scenarios** — 192 paced, 640 LT, in `cmd` and
  `cmd_loop` alike. The scenarios differ in what happens *after* the shot arrives, not in how long
  it takes to arrive.
* **A handover is not a startup.** 128 samples, and **identical paced and unpaced**. The lead is a
  standing offset built once; a handover happens with the pipe already full, so the lead contributes
  nothing to it. One number does not cover both cases and must not be asked to.
* `cmd`'s finite playout has **no** handover at all, so `cmd_loop` is the only scenario that
  measures one.

Nothing is lost on the path. In all four pysim runs `StreamIF.dropped == 0` on both channels and
`RFSampIF` reports `underrun == overrun == 0`; on both RTL runs `DAC_BLOCKS_ZERO_FILLED == 0`, which
is the gate that already asserts it. So the transient is a pure **prefix**, not loss — every filler
sample in it was written by the player, and every one the player wrote arrived.

### 3. Does the measurement sit under the bound? **Yes, at equality.**

Every stage's occupancy at the instant the player writes its first *real* chunk — `cmd`, LT
(`cmd_loop` is identical to the sample):

| stage | occupancy | its bound |
|---|---|---|
| 1 `tb_dut_samp_if` queue | 64 | 64 |
| 2 `RfRelayoutToSlots` in flight | 64 | 64 |
| 3 `tb_dac_axis` queue | 128 | 128 |
| 4 `Rfdc._dac_proc` in flight | 64 | 64 |
| 5 `RFSampIF._buf` | 128 | 128 |
| 6 sink `rx_queue` | **0** | 128 declared |
| **total in flight** | **448** | **448** |

At that same instant the grid had already delivered **192** samples, so

```
transient_startup(LT)  =  C_lead  +  drained_before_the_shot_arrived
              640      =    448   +   192
```

— and `192` is exactly what the paced run and the RTL both measure. Under pacing the same snapshot
reads **0 in flight at every stage**: the player never gets ahead, and the transient is the 192
alone.

Each term is load-bearing, checked by sweeping it with the metronome off (`cmd`):

| `D_dac` (words) | 64 | 32 | 16 | 8 | 4 | 2 |
|---|---|---|---|---|---|---|
| in flight at the switch | 512 | **448** | 384 | 384 | 384 | 384 |
| startup transient | 640 | **640** | 576 | 576 | 576 | 576 |

| `D_rf` (blocks) | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| in flight | 384 | **448** | 512 | 512 |
| startup transient | 576 | **640** | 640 | 640 |

| `D_rx` (blocks) | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| in flight | 448 | **448** | 448 | 448 |
| startup transient | 640 | **640** | 640 | 640 |

`D_dac` steps at 16 rather than sliding, which is `max(D, B)`: below the burst size a channel carries
one burst whatever its depth. `D_rf` saturates past 3 blocks and `D_dac` past 32 because the player's
own fabric write rate — one word per cycle at 250 MHz — becomes the binding constraint before the
queue does. The sum is a **bound**, and at the gated depths it is reached exactly.

### 4. Where the 128 went — **two errors that cancelled**

The plan predicted "~448 against the **576** S3 measured" and called the difference a 128-sample gap.
Both numbers were wrong, in opposite directions, and that accident is why it read as one clean gap:

* **The 576 was a misquote.** `plans/pysim_burst_backpressure.md` S3 measured the gated geometry at
  **640**, and says so in its own table (*lead filler … without it: **640***). The 576 is one row of
  S3's `dac_axis` **depth sweep** — the value at `D_dac <= 16`, which is not the gated configuration.
  The sweep in §3 above is that same sweep, reproduced, and it agrees line for line.
* **The four-term list was two stages short and one stage long.** It had the `dac` FIFO, the Rfdc's
  in-flight block, `RFSampIF._buf` and the sink queue: `128 + 64 + 128 + 128 = 448`. Measurement says
  the sink queue holds **0**, and that two stages nobody had listed hold **64 each** — the composite's
  own `samp` FIFO between the player and the re-layout, and the re-layout task itself. `-128 + 128 =
  0`, so the total was right for the wrong reasons.

**The re-layout was the first place to look and it was half the answer.** The other half is the
`samp` FIFO in front of it, and that one is visible only if you read the depth *and* the burst size:
its declared depth is 2.

### 5. What actually needs a guard, and what does not

**The depth sum does not bound the transient. It bounds the lead.** The remaining 192 samples are the
design's own load latency — driver, header, 64 payload words, lock acquire, grant — expressed on the
converter's grid, and no queue depth is an input to it. A bound written as *"the transient is under
the sum of the depths"* is false as stated: at this geometry the transient is 640 and the sum is 448.

What does hold, and what both backends satisfy:

```
transient_startup(LT)  <=  C_lead + transient_startup(RTL)
transient_handover(LT)  =  transient_handover(RTL)          # the lead is built once
```

**But gate 2 needs neither.** It aligns each stream on *its own* log, and the lead is a constant
prefix that the alignment removes. Measured, both scenarios, with `guard = 0`:

| aligned comparison | identical |
|---|---|
| RTL vs pysim paced | yes — this is what the current `array_equal` gate asserts |
| RTL vs pysim **LT** | **yes** |
| pysim paced vs pysim LT | **yes**, all 1088 samples after the first transition |

So at this geometry the whole LT↔RTL disagreement *is* the prefix, and once the log removes it
nothing is left over. `guard` is insurance against a geometry where the alignment lands a sample or
two off — not against the 448.

### 6. Two things S2 inherits

**The LT run needs a longer horizon or it loses its tail.** The lead is 448 samples = 7 blocks and
the run is a fixed 20, so with the metronome off `cmd` never reaches its third pass or its trailing
quiet — the playout is truncated by the horizon, not by the design. Raising `n_blk` from 20 to **27**
restores it exactly:

```
paced,  n_blk=20:  [(F,192), (P,768), (F,320)]
LT,     n_blk=20:  [(F,640), (P,640)]                 <- tail lost
LT,     n_blk=27:  [(F,640), (P,768), (F,320)]        <- identical but for the prefix
```

`27 = 20 + ceil(C_lead / blksize)`, derived from the same formula. **Assumption recorded** (not in
the plan, taken here from its reasoning): S2 raises the pysim horizon by `ceil(C_lead / blksize)`
blocks rather than by a chosen number, so it tracks the depths for the same reason `guard` does.

**The RTL's own lead is below this edge's resolution.** The RTL startup transient equals the paced
pysim's to the sample (192), and the paced pysim's lead measures 0, so the RTL's lead is under one
block. It cannot be measured finer from the played stream — a block-LT sink cannot see inside a
block. Stated as an inference, not a measurement.

### What S1 did not touch

No gate, no assertion, no constant. `test_both_backends_agree_sample_for_sample`, the phase check,
and `dac_word_rate` are all exactly as they were; the suites are unchanged (6 non-vitis failures,
87 XSI gates, 0 skipped).

## S2 as built — **three gates, one number moved, and the design did not**

Built 2026-09-07, branch `lt-transient-s2`. `ShotTxPlayer.dac_word_rate` and `RfShotTx.dac_word_rate`
are gone; `RfSampBufPlayer.dac_word_rate` and `RfTxStream.slot_period` remain, for the measured
reasons `plans/pysim_burst_backpressure.md` S3 records.

### The three gates

All three live in `tests/examples/test_rf_shot_tx_xsi.py`; the vocabulary they call
(`check_phase`, `compare_after_transients`, `play_log`, `transients`, `c_lead`) is in
`examples/rf_shot_tx/rf_shot_tx.py`, beside `segments()` — **the log and the comparison are one
mechanism and belong together.** *(Assumption recorded: the plan writes
`compare_after_transients` as a free function without saying where it lives. Nothing outside this
example derives a log from its own output yet, so it is not framework until a second caller exists.)*

| | gate | what it asserts | items |
|---|---|---|---|
| 1 | `test_every_playout_segment_is_in_phase_with_the_waveform` | `real[i] == shot_codes(base)[i % nsamp]` inside every playout run, **per backend, per segment** | 4 |
| 2 | `test_the_two_backends_agree_after_their_own_transients` | same event sequence, then every playout run compared **exactly** after `GUARD` samples | 2 |
| 3 | `test_the_transients_are_the_recorded_ones` | startup and handover pinned **per backend**, separately | 4 |
| — | `test_the_lt_lead_sits_under_the_bound_the_declared_depths_derive` | `startup(LT) <= c_lead + startup(RTL)`, and `handover(LT) == handover(RTL)` | 2 |

**Gate 1 is what makes gate 2's relaxation safe**, and it is stronger than what it supplements in two
places: it runs on the **pysim** capture, which nothing checked directly before, and it covers the
ragged **tail** — `check_finite_playout` / `check_loop_playout` truncate to whole passes
(`whole = size - size % want.size`) and never look at a final partial pass the horizon cut.

**Gate 1 asserts the segment's base is a *declared* waveform** (`KNOWN_BASES`) before checking phase
against it. Deriving the base from the run's own first sample would make the check tautological at
`i = 0` and unable to catch a run that started mid-waveform — which is exactly the read-pointer error
it exists for.

**Gate 2's event is the waveform's base code**, so the log says *what* happened and not merely how
many times something did: a run that played `[A, B]` against one that played `[A, A]` fails on the
event sequence rather than being aligned and then compared. *(Assumption recorded: the plan's
`log_a = [(e, i), ...]` does not say what `e` is.)*

**Gate 2 compares per playout run, not from each event to the end of the stream.** S1 measured the
stronger whole-tail form and it also holds, but per-run keeps the concerns apart: gate 2 owns
*values*, gate 3 owns *transient lengths*. Under the whole-tail form a handover that changed length
would fail gate 2 with a confusing message about a value.

### The speculative-read claim survived

The retired gate's docstring named it as *"the honest half"* of
`test_the_handover_leaves_a_speculative_read_that_the_design_discards`: pysim takes the region out of
the owner's hands inside `grant()` and **raises** on the very next access, so if the RTL player were
*using* the words it speculatively reads while yielded, the two sequences could not agree. Gate 2
compares the same values against the same pysim run, so the proof is intact — **alignment moves
*where* the comparison starts, never *what* it compares.** Said so in gate 2's docstring, and the
speculative-read test's own docstring now points at gate 2 and records that the alignment does not
weaken the evidence it is leaning on.

### `guard = 0`, and it stayed a parameter

S1 measured RTL against the LT pysim capture, aligned on their own logs, identical over all 1088
samples after the first transition in both scenarios. So `GUARD = 0` and no machinery was built for a
number the measurement says is unnecessary. It stays a parameter as insurance against a geometry
where a boundary lands a sample or two off — never as cover for the lead, which the log removes.

### Every number, and whether `n_blk` explains it

**`n_blk` 20 → 27 is the one number S2 changed**, and it is derived:
`N_BLK_BASE + ceil(c_lead / blksize)` = `20 + ceil(448 / 64)` = `27`. Without it the LT run loses its
tail — the third pass and the trailing quiet are still in the pipe when the horizon closes, so the
playout is truncated by the testbench rather than by the design.

**It is written down and *gated*, not computed at import.** `c_lead()` needs a bound graph and
`n_blk` is a field default of the very testbench that graph comes from, so deriving it at import
would be circular. `check_horizon_covers_the_lead()` closes that: one formula, in `c_lead`, and a
check that the recorded number still follows from it. Worth having because the failure is quiet — a
horizon that stopped covering the lead does not raise, it just ends the run with part of the playout
in flight, and the capture then looks like a design that truncated itself.

Moved, **all of them downstream of `n_blk` by construction**:

| number | before | after | why |
|---|---|---|---|
| pysim `played_samples`, both scenarios | 1280 | 1728 | 27 blocks instead of 20 |
| pysim `blocks_delivered` | 20 | 27 | same |
| pysim startup transient | 192 | 640 | the metronome is gone: 192 (load latency) + 448 (`C_lead`) |
| pysim trailing filler, `cmd` | 320 | 320 | *unchanged* — the tail is back, exactly |
| pysim trailing filler, `cmd_loop` | 832 | 832 | *unchanged* |
| `docs/examples/rf_shot_tx/images/playout.svg` | — | regenerated | drawn from the pysim run; the leading shaded run is now the lead |
| `WANT_XSI_GATES` | 87 | 97 | `test_rf_shot_tx_xsi.py` 20 → 30 gates |

**Did not move — and this is the claim the plan made:**

* every RTL number. `WANT_SEGMENT_BLOCKS`, `WANT_RESP_LAST_CYCLE` (269 / 500), `WANT_DAC_WORDS`
  (359), `WANT_ZERO_FILLED` (0), `WANT_DAC_UNDERRUN` (1 at cycle 4), `WANT_PORT_OVERLAP_CYCLES`
  (18 / 55), `WANT_RDW_COLLISIONS` (0 / 2), `WANT_WRITE_RANGE`, every II.
* the generated C++. `gen/rf_shot_tx.cpp` and `include/` regenerate **byte-identical**, so
  `rtl_staleness` still reports clean and no csynth was needed. That is the proof `dac_word_rate`
  never reached hardware.
* pysim `n_plays`, `n_done`, `grants`, `underrun`, `overrun`, and every verdict on both streams.
* all 17 gates in `tests/hw/test_rf_shot_tx.py`, unchanged assertions.

### The bench that had no converter

`tests/hw/test_rf_shot_tx.py`'s `Bench` has no `Rfdc` on purpose, and it took the player's metronome
directly. With the field gone the pacing had to come from somewhere, and **the honest somewhere is
the consumer**: `DAC_WORD_RATE` now paces `Bench._drain` — one chunk per chunk period, on an absolute
grid — so the player is back-pressured exactly as it is at RTL. Throughput is unchanged at one 4-word
chunk per microsecond, every scenario timing in that file still lands where it did, and all 17
assertions pass untouched. *(Assumption recorded: the plan says the parameter goes from the player
and does not say what a converter-less bench should do instead. Moving a converter's rate onto the
model of a converter is what its own reasoning implies.)*

### Two smaller things

**The stale docstring is fixed.** `ShotTxPlayer` no longer claims *"pysim does not back-pressure a
burst write"*; `_chunk_and_pace` is now `_chunk`, because after the change the name was a lie.

**`blk_words` — stated accurately rather than as the plan phrased it.** The plan says it "becomes
only the lock poll period". It does not, quite: the body still writes `blk_words` per burst, and it
must, because the converter edge downstream takes a whole block per event and refuses a partial one.
What it stopped being is a **rate** — the quantum a metronome divided. The field's docstring now says
that, rather than claiming a decoupling the code does not have. Decoupling the burst width from the
poll period would mean changing the re-layout↔`Rfdc` burst contract, which is a different piece of
work and not this plan's.

**The golden's non-zero start is a check now**, `check_golden_is_loggable`, run at import so no
consumer can build a golden the log cannot segment. It **raises** rather than asserts, because
`python -O` strips an `assert` and this one is load-bearing.

### Also fixed in passing

`docs/guide/rf/rfshotbuf/tx_internal.md` cited five line ranges in `rf_shot_tx.py` that were
**already wrong at `main`** — `rf_shot_tx.py:815-822` was said to be the channel table and was
`self.n_chunks += 1`, about 110 lines off. S2 shifted the file another seven lines, so they were
re-anchored rather than left. Not a defect S2 introduced.

---

## Stages

### S1 — measure the bound, change no gate — **DONE**

Compute the guard from the declared depths along the path. Measure the actual transient on both
backends, both scenarios. **Report whether the measurement sits under the computed bound**, and if it
does not, find the stage the 128-sample gap lives in.

The deliverable is a number and an explanation, not a gate. If the bound cannot be accounted for, say
so — a formula that does not match reality is worse than no formula.

**Result in *S1 as measured*.** The bound holds at equality; the gap was a misquoted measurement plus
a two-stage omission that cancelled it.

### S2 — the three gates, and the retirement — **DONE**

Phase per segment; agreement aligned on the log; transient recorded. Assert the golden starts
non-zero. Then `dac_word_rate` goes from `ShotTxPlayer` and `blk_words` loses its second meaning.

**Expect the design's numbers not to move** — nothing here changes what the design does.

**Result in *S2 as built*.** They did not: the generated C++ is byte-identical and every RTL number
holds. One number moved — `n_blk` 20 → 27, derived as `20 + ceil(C_lead / blksize)` — and everything
else that moved is downstream of it. `blk_words` is recorded as it actually is rather than as the
plan phrased it: it stopped being a *rate*, and it is still the burst width, because the converter
edge takes a whole block per event.

### S3 — Matched mode

Only if wanted. The `Rfdc` emits a timing event derived from `tx_samp_rate`.

## Not in scope

- `RfSampBufPlayer` and `RfTxStream`'s pacing. Kept for measured reasons.
- The word-granular `data_buffer` question — the 6 internal channels under-counting their stalls.
- **Where `Rfdc` lives.** It is defined in `waveflow/hw/rfdc.py` and imported by seven RF
  designs and a whole guide section, while its siblings `RFSampIF` and `RfdcSampWord` are framework.
  A real structural question, and not this plan's.
