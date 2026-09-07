# Plan — loosely-timed by default, matched on demand

**Status: DECIDED 2026-09-07. S1 MEASURED 2026-09-07; nothing built.** Owns what the pysim↔RTL
comparison asserts about *timing*, and the metronome parameters that exist to satisfy the current
answer. **Replaces `plans/filler_offer.md`**, which proposed a player-side fix for something the
player was not causing.

---

## Next session starts here — S2

**S1 is DONE** (2026-09-07, branch `lt-transient-s1`) — see *S1 as measured*. It changed no gate and
no constant. The bound is `C_lead = 448` samples at the gated geometry, the measured lead is 448
exactly, and the plan's "128-sample gap" was two errors that cancelled.

```
claude "Read plans/lt_transient.md, sections 'What the gates become' and 'S1 as measured',
        and build S2.  The formula and every number S2 needs are in 'S1 as measured'."
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

## What retires

* `ShotTxPlayer.dac_word_rate`, and the hand-computed `samp_rate / samp_per_word` in the example.
* `blk_words`'s second meaning — it becomes only the lock poll period.
* The other two metronomes stay for their own measured reasons, recorded in
  `plans/pysim_burst_backpressure.md`: `RfSampBufPlayer.dac_word_rate` is `max(fabric, demand)` and
  models which side is the bottleneck; `RfTxStream.slot_period` raises when unset and is a guard.

## Traps

**Relaxing a gate must not stop a measurement.** Gate 3 exists for this: the transient stays pinned
per backend even though it is no longer cross-compared.

**The stale docstring.** `rf_shot_tx.py:689-690` still says *"pysim does not back-pressure a burst
write"*. S2 made that false. Fix it with this work.

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
| 4 | — | `tx_blksize // SPW` = `B` | `Rfdc._dac_proc`, `examples/rf_loopback/rfdc.py:579` — one block held across `tx_rf.put` |
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

### S2 — the three gates, and the retirement

Phase per segment; agreement aligned on the log; transient recorded. Assert the golden starts
non-zero. Then `dac_word_rate` goes from `ShotTxPlayer` and `blk_words` loses its second meaning.

**Expect the design's numbers not to move** — nothing here changes what the design does.

### S3 — Matched mode

Only if wanted. The `Rfdc` emits a timing event derived from `tx_samp_rate`.

## Not in scope

- `RfSampBufPlayer` and `RfTxStream`'s pacing. Kept for measured reasons.
- The word-granular `data_buffer` question — the 6 internal channels under-counting their stalls.
- **Where `Rfdc` lives.** It is defined in `examples/rf_loopback/rfdc.py` and imported by seven RF
  designs and a whole guide section, while its siblings `RFSampIF` and `RfdcSampWord` are framework.
  A real structural question, and not this plan's.
