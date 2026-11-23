---
title: Timing fidelity
parent: Evaluation
nav_order: 3
has_children: true
summary: "Is Waveflow's pysim right, and what does checking it find? Two claims: pysim's cycles match the RTL's within a stated tolerance, for compositions of calibrated blocks and not only for the blocks alone; and where they do not, attributing the difference finds real performance bugs -- the calibrated simulator as a performance oracle. The method (an accuracy gate; per-frame timelines in both backends; the gap attributed hop by hop; every discrepancy classified as a pysim error, a real cost, a design flaw or a wrong conclusion drawn from the RTL alone), and the ledger of every discrepancy found so far: of 21, 5 were design flaws and 3 wrong conclusions that RTL numbers alone had not exposed. Weaknesses: one loop and its blocks so far, and the RTL-only counterfactual not yet tested."
---

# Timing fidelity

[Simulation speed](../sim_speed/index.md) asks whether pysim is fast enough to be the design loop. This
section asks the other half: **is it right** -- and what does checking it find?

## The claims

1. **pysim's cycles match the RTL's within a stated tolerance, for compositions, not only for blocks.**
   Each block is calibrated alone; a design is a composition. Whether block-level calibration carries over
   to compositions it was not measured in is the key technical claim of the architecture study
   (`plans/dse_arch.md`, *held-out topologies*). The first loop checked did not, at first: it does now,
   within 2.7% and 0.6%, once its calibration covered the patterns the loop makes and every task in it
   was timed ([os_filterbank](os_filterbank.md)) -- so the claim holds with that condition, on one design.
2. **Where pysim and the RTL disagree, attributing the difference finds real performance bugs.** A
   designer working only in RTL has the right cycle counts but no statement of what they *should* be, so a
   slow design and a wrong explanation both pass unnoticed. A calibrated simulator is an executable
   statement of the expected cost of every block, checked against the RTL by a gate: a disagreement is a
   signal, and attributing it localises the cause. Call it a **performance oracle**.

The comparison is against what designers actually do -- RTL simulation, read by a person -- and the
weakness of claim 2 is stated with it (*What is not shown*, below).

## The method

1. **A gate per design**: pysim's cycles against the RTL's at the points the design's RTL gates already
   pin, both directions, a stated tolerance. A design without one can drift unnoticed: `os_filterbank`'s
   pysim ran 9% short, then 27%, while every gate passed, because the gates compared words, not cycles.
2. **When the gate fails, a per-frame timeline in both backends**: for every frame, keyed by its tag, the
   cycle its first and last word cross each link, from a traced RTL run (`waveflow.utils.trace`) and from
   pysim's own events.
3. **The gap attributed hop by hop** along the RTL's critical path, each hop charged to a cause, the causes
   summing to the gap. A cause is fixed only once explained; a constant that absorbs unexplained cycles is
   never added.
4. **Every discrepancy classified**:

   | class | meaning |
   |---|---|
   | **design flaw** | the hardware is worse than it should be, and can be fixed |
   | **real cost** | the hardware spends cycles the model did not charge; not a flaw, now modelled |
   | **pysim error** | the model spent cycles the hardware does not, or moved data differently |
   | **wrong conclusion** | a claim or explanation drawn from RTL results alone that the attribution showed false |

The tool for steps 2-3 is `examples/os_filterbank/fidelity.py` (`--timeline`, `--attribute`); the
stream blocks' calibration is `waveflow/calib/fixtures/stream_blocks.py`.

## The ledger

Every discrepancy found so far, oldest first. Measured with Vitis HLS / Vivado / xsim 2025.1 throughout.

| # | date | design | what disagreed | class | how it was found | fix | record |
|---|---|---|---|---|---|---|---|
| 1 | 2026-10-19 | systolic unit | every job serialized: its receiver-to-store channel was 2 words deep against a 3-word reply header, so job n+1's load waited for job n's store | design flaw | timing the unit's phases for its calibration | the channel two headers deep: **821 -> 390 cycles a job** (8x8x32, back to back) | commit `c008cd7` |
| 2 | 2026-10-19 | systolic unit | the 2024.1 cycle model's "transfers add" explanation was measuring #1's serialization, not the memory interface | wrong conclusion | the same | the explanation replaced | commit `c008cd7` |
| 3 | 2026-11-07 | LTBF system | HLS masters issue 16-beat bursts and the shared memory serves one at a time (`2n + 2` cycles a read burst); the vector unit's writer waits for each burst's write response (21 cycles a 16-beat burst); pysim read a transfer as one transaction | real cost | the system gate 5-12% off; RTL traces | modelled: pysim within 1.5% of RTL at every memory setting | `plans/dse_cg.md`, step 1f |
| 4 | 2026-11-22 | `StreamArbiter` | pysim ran; the RTL hung from 3 inputs (4 in the arbiter-demux pair): Vitis kept one copy-loop module for every input, its stream muxed per call | design flaw | the RFSoC resource sweep's extra configurations | the copy loop `INLINE`, one per input | `plans/shared_kernel_rfsoc_timing.md` |
| 5 | 2026-11-22 | `StreamArbiter`, `StreamDispatch` | the RTL spends 3-8 idle cycles a frame (arbiter) and 3-15 a job, 4-12 a credit (dispatcher) deciding; pysim spent 1 and 0 | real cost | measuring the blocks' cost per frame | a calibrated lookup per block, opt-in by platform | [Stream blocks](stream_blocks.md) |
| 6 | 2026-11-22 | `StreamDispatch` | round-robin's search costs 10 and 20 cycles a job at 8 and 16 workers, against first-free's 5 | design flaw (minor) | the same | accepted for now; a faster search written down | `plans/stream_dispatch.md`, open question 3 |
| 7 | 2026-11-22 | `StreamDispatch` | at one credit a worker, first-free at RTL sends every job to worker 0 | real cost (a load-balance behaviour) | comparing which worker took each job, RTL against pysim | modelled (#8); stated on the block's page | [Stream blocks](stream_blocks.md) |
| 8 | 2026-11-22 | both blocks | pysim moved a burst whole; the RTL commits on a frame's first word and copies as words arrive, so pysim returned credits late and spread first-free's jobs 20 / 20 | pysim error | #7 | both blocks commit on a begun burst | [Stream blocks](stream_blocks.md) |
| 9 | 2026-11-22 | both blocks | inside a composite a firing costs 2-4 cycles less than alone, so a bare timed pool runs 6-15% long | pysim error (calibration context) | the pool's end cycle against the RTL | open: calibrate a design in context | `plans/stream_dispatch.md`, open question 5 |
| 10 | 2026-11-22 | `os_filterbank` | #8's fix moved the bank's pysim **away** from its RTL (-9% to -17%, -20% to -27%): the arbiter's store-and-forward, a cost the RTL does not have, had been hiding a larger gap | pysim error (masking) | asking which way the end cycle moved | closed: attributed (rows 11-21) and fixed; each channel within 2.7% (`test`) and 0.6% (`long`) of the RTL, gated at 3% / 1% | `plans/stream_dispatch.md`, Stage 8 record; [os_filterbank](os_filterbank.md) |
| 11 | 2026-11-22 | `SsrFft` | after any gap of 1 to `L/R` cycles the RTL's frame interval is `2L/R`, not `L/R + gap`: half rate. The last transposer commutator commits to a frame-long bubble group | design flaw | #10's attribution: 92-103% of the gap | modelled; the HLS fix is `plans/ssr_fft_gap_throughput.md` | `plans/osfb_pysim_fidelity.md`, Section 6 |
| 12 | 2026-11-22 | `SsrFft` | its documented interval, "`L/R`, by construction", held only for the patterns measured (back to back, isolated) | wrong conclusion | #11 | `docs/guide/dsp/ssr_fft/timing.md` corrected | the same |
| 13 | 2026-11-22 | `os_filterbank` | `pre` leaves a cycle or more between frames, which is what puts the FFT at half rate (#11) | design flaw | #11's trace | open, in progress (2026-11-23): `pre`'s body rewritten to feed the FFT back to back when a frame waits (one II=1 loop, the tag's cycle between words 0 and 1) and its model with it; not yet at RTL. pysim cannot show the effect -- its `SsrFft` never stalls `pre`, so no frame waits there -- a decision for the user | `plans/ssr_fft_gap_throughput.md`, fix 1 |
| 14 | 2026-11-22 | `os_filterbank` | its page said `post` was why the FFT is under half busy; the FFT's own half rate after a gap is | wrong conclusion | #11's attribution | the page corrected | commit `3b20f22` |
| 15 | 2026-11-22 | `os_filterbank` | `pre`, `post` and `front` were charged only their links' word rate in pysim (causes B, C, D) | pysim error | #10's attribution | each charges what its HLS body does | commit `3b20f22` |
| 16 | 2026-11-22 | `StreamDemux` | its decision cycles are untimed in pysim, and it stores and forwards where its body copies through (causes F, H) | pysim error | #10's attribution | closed: the demux commits on a frame's first word and copies through (as #8 for the arbiter), and a decision table, opt-in by platform: 3 idle cycles a frame at every point measured; the bank's F share +4 -> -2 (`test`), +3 -> 0 (`long`), H -12 -> -2 | `plans/osfb_pysim_fidelity.md` |
| 17 | 2026-11-22 | `StreamArbiter` | its decision table was measured at 32 bits and 1-burst frames; at 128 bits and 2 bursts every gap is a cycle longer | pysim error (calibration coverage) | the spot check; #10's attribution (cause E) | closed: the arbiter's table keyed by width and frame (`w`, `fb`), calibrated at the bank's 128 bits and 2-burst frames (4 / 5 cycles, priority / round-robin, against 3 / 4); the bank's E share +5 -> +2 (`test`), +1 -> 0 (`long`) | `plans/osfb_pysim_fidelity.md` |
| 18 | 2026-11-22 | `SsrFft` (pysim) | #11's rule counted a gap from the previous frame's start -- exact for a driver the FFT stalls -- and in `os_filterbank` charged the loop's own back-up twice (+17-20%); counted from the arrivals the bank lands on its RTL (-2.2%, +0.1%), but a driver the FFT stalls is early: 4-8% short on random gaps, since pysim's links move a burst at a time and do not stall a source word by word | pysim error (link back-pressure) | #11's model against the bank and the driver runs | counted from the arrivals; the driver's limit stated and pinned; word-by-word back-pressure (the same root cause as #8 and #16) DECIDED (rejected, 2026-11-22, the user): too slow, it defeats the point of pysim -- pysim keeps moving bursts, and the driver's 6-8% stays a named limit, not modelled | `plans/osfb_pysim_fidelity.md`, Sections 7-8 |
| 19 | 2026-11-22 | `SsrFft` at `L = 16` | no rate lost after a gap, but each frame's latency moves 40-43 cycles with the gaps before and after it (arrival phase); at a constant 1-3 cycle gap the RTL falls up to 13 cycles behind `L/R + g` over 11 frames | real cost (arrival phase) | #11's gap sweep at every `L` | stated, not modelled: within 3 cycles a frame, exact in total on random gaps | `docs/guide/dsp/ssr_fft/timing.md` |
| 20 | 2026-11-22 | `os_filterbank` | `pre` spends 6 cycles from a frame's tag word to its first word into the FFT (its `COPY` loop's entry); pysim charged 3, the residual of #15's cause B (+9 on `test`) | real cost | Step 3 again, after #16 and #17 | modelled: `STAGE_CYC["pre_copy"]`; B +9 -> 0, the bank `test` -2.7% -> -2.0%, `long` -0.1% -> exact | commit `b5c65bc7` |
| 21 | 2026-11-22 | `os_filterbank` | `post` begins its last inverse frame on `test` 38 cycles after the FFT's first word of it at RTL, 24 in pysim: 14 of the 23 cycles left at `test`'s end event (cause C; none on `long`) | not yet explained | Step 3 again, after #20 | open | `plans/osfb_pysim_fidelity.md`, Step 5 |

**So far:** 5 design flaws (#1, 4, 6, 11, 13), 5 real costs (#3, 5, 7, 19, 20), 7 pysim errors (#8, 9, 10,
15, 16, 17, 18), 3 wrong conclusions (#2, 12, 14) and 1 not yet explained (#21). Every design flaw and
wrong conclusion was in hardware or documentation that had passed its RTL gates. By status: 14 closed
(fixed, modelled or corrected); #11 modelled in pysim, its hardware fix planned; #6 accepted; #18 and #19
stated limits, not modelled; 3 open (#9, 13, 21).

`os_filterbank` has its own page, [Timing fidelity: os_filterbank](os_filterbank.md): the attribution, each
fix with its before and after, the gate and its tolerance, what it cost in speed and what remains. Its
lessons for the next composite are a checklist in the guide,
[Timing a composite](../../guide/timing_model/composites.md).

## What is not shown

- **One loop and its blocks.** Rows 5-21 come from one week, one example and the blocks it uses. Whether
  the rate of real findings holds on other designs is unmeasured.
- **Selection.** The method finds only what pysim and the RTL disagree on. A flaw both share -- a model
  written from the same wrong assumption as the RTL -- passes. Row 12 is the warning: the FFT's model
  and its documentation agreed, because both were checked on the same narrow patterns.
- **The counterfactual is not tested.** Claim 2 says an RTL-only designer would miss these, or pay more to
  find them. That is argued from rows 2, 12 and 14 (each a person reading RTL results and concluding
  wrongly), not measured. An experiment would give a person or an agent only the RTL of `os_filterbank`
  and ask why its FFT is under half busy, and record what they find and what it costs.
- **The cost of the method is not totalled.** Each row's effort is in its plan's progress log; a total
  (agent hours, RTL hours) against the findings would make claim 2's trade explicit.

## The pages

- [Stream blocks](stream_blocks.md) -- `StreamArbiter` and `StreamDispatch`: their decision cycles, word
  against burst, the pool's fidelity, and what each fix cost in simulation speed.
- [os_filterbank](os_filterbank.md) -- a loop around one shared FFT: from 16% and 26% short to within 2.7%
  and 0.6% per channel, cause by cause, gated; 1-4% more wall time, no word-level events.
