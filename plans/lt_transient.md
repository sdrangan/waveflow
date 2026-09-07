# Plan — loosely-timed by default, matched on demand

**Status: DECIDED 2026-09-07, NOTHING BUILT.** Owns what the pysim↔RTL comparison asserts about
*timing*, and the metronome parameters that exist to satisfy the current answer. **Replaces
`plans/filler_offer.md`**, which proposed a player-side fix for something the player was not causing.

---

## Next session starts here — S1

```
claude "Read plans/lt_transient.md, section 'S1', and build it.
        S1 MEASURES the transient bound and does not change a gate.  The guard is
        DERIVED -- see 'The guard, which must be derived and not chosen'."
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

## Stages

### S1 — measure the bound, change no gate

Compute the guard from the declared depths along the path. Measure the actual transient on both
backends, both scenarios. **Report whether the measurement sits under the computed bound**, and if it
does not, find the stage the 128-sample gap lives in.

The deliverable is a number and an explanation, not a gate. If the bound cannot be accounted for, say
so — a formula that does not match reality is worse than no formula.

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
