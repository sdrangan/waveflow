---
title: "Timing fidelity: os_filterbank"
parent: Timing fidelity
nav_order: 2
summary: "The shared-FFT filter bank, pysim against RTL (Vitis HLS / xsim 2025.1, xczu48dr at 250 MHz). Every RTL gate passed while its pysim ran 16% (test) and 26% (long) short. Attributed hop by hop along the RTL's critical path: 92-103% of the gap was SsrFft's interval after a gap, a rule its block-level sweep had never measured; the rest the example's own stages (charged only their links' word rate, one stored and forwarded) and the stream blocks (the arbiter's width, the demux untimed and store-and-forward). Seven steps of fixes later the bank is within 2.7% (test) and 0.6% (long) per channel, gated at 3% / 1%, for 1-4% more wall time and no word-level events. One cause, 14 cycles of a post wait, is open."
---

# Timing fidelity: os_filterbank

[`examples/os_filterbank`](../../examples/os_filterbank/index.md) is a three-channel overlap-save filter bank
on **one** `SsrFft`: each block goes through it forward and back inverse, around a loop of eight tasks -- the
channels' `front`s, a `StreamArbiter`, `pre`, the FFT, `post`, and three `StreamDemux` returning answers,
statuses and credits. Its RTL gates compared words, so they passed while its pysim ran **16% (`test`) and
26% (`long`) short** of the RTL (ledger row 10). `plans/osfb_pysim_fidelity.md` found why and closed it; a
gate keeps it closed. Everything here: the lab machine, Vitis HLS / Vivado / xsim 2025.1, the RFSoC 4x2's
xczu48dr at 250 MHz; cycles on the XSI testbench's clock.

## The method, applied

`python -m examples.os_filterbank.fidelity` (`--rtl` for the traced RTL run, `--timeline`, `--attribute`):

1. The end cycle and each channel's last status, RTL against pysim.
2. A per-frame timeline in both backends: every frame, keyed by its tag `(ch, tx_id, blk, npass)`, the cycle
   its first and last word cross each of the bank's links -- 246 frame-links on `test`, 664 on `long`, the
   same set in both.
3. The gap attributed along the RTL's critical path, hop by hop (each hop the predecessor with least slack),
   each hop charged to a cause; the causes telescope, so they sum to the end-cycle gap **exactly**.

## The attribution

At the start (the arbiter timed on the platform, the only block with a table; cycles RTL minus pysim):

| cause | `test` | `long` |
|---|---|---|
| A. `SsrFft`'s interval after a gap: RTL `max(2L/R, L/R + g)`, pysim `L/R + g` | +157 (95%) | +643 (103%) |
| A2. `SsrFft`'s latency (within its documented 5 cycles) | +3 | +1 |
| B. `pre`: pysim waits for the whole frame, the RTL copies it through | -39 | -13 |
| C. `post`: its loops' overheads; its inverse pass copied through at RTL | +21 | -11 |
| D. `front`: after a credit, the RTL reads the block's 12 words before the frame | +15 | 0 |
| E. the arbiter's decision at 128 bits and 2-burst frames (its table: 32 bits, 1 burst) | +5 | +1 |
| F. the demuxes' decision cycles (untimed in pysim) | +4 | +3 |
| G. the boundary: when the first frame starts | +12 | +12 |
| H. pysim's last word is another event than the RTL's (`dmx_ans` stored and forwarded) | -12 | -12 |
| **the gap** | **166** | **624** |

**Cause A.** `SsrFft` alone at RTL (`--fft-gaps`, `L = 64`, every frame bit-exact) runs a frame every 16
cycles back to back and every **32** after any gap of 1 to 16 cycles: a gap costs a whole frame-time, set
by the last transposer commutator. It was measured only back to back and on isolated frames before, where
both are exact, and its guide page said "`L/R`, by construction" (row 12). `pre` always leaves a cycle
between frames, so the RTL's FFT runs at half rate throughout: 72 frames of 32 cycles plus the fill is
`long`'s 2,424. This is also a real loss in the hardware (row 11; its fix is
`plans/ssr_fft_gap_throughput.md`).

## Each fix, before and after

End cycle, the stream blocks timed (the arbiter until F; the arbiter and the demuxes after):

| step | the fix | where | `test` (RTL 1,067) | `long` (RTL 2,424) |
|---|---|---|---|---|
| start | -- | -- | 901 (-15.6%) | 1,800 (-25.7%) |
| A | the rule on `tp{S-2}` at `L >= 64`, the gap counted from the previous frame's start | `SsrFft`'s model | 1,228 (+15.1%) | 2,540 (+4.8%) |
| B | `pre` copies the frame as its words arrive (`copy_burst`, as the HLS `COPY` loop) | the example | 1,212 (+13.6%) | 2,524 (+4.1%) |
| C, D | `post` and `front` charge their loops' entries and exits, read off the trace (`STAGE_CYC`), and read a block a word a cycle | the example | 1,279 (+19.9%) | 2,841 (+17.2%) |
| A, counted | the gap counted from the arrivals: a frame starts no earlier than it arrives, nor than `2L/R` after the previous start (the user's option (a)) | `SsrFft`'s model | 1,043 (-2.2%) | 2,427 (+0.1%) |
| E | the arbiter's table keyed by width and frame, calibrated at 128 bits and 2-burst frames: 4 / 5 idle cycles (priority / round-robin), against 3 / 4 | stream block | 1,045 (-2.1%) | 2,428 (+0.2%) |
| F, H | `StreamDemux` commits on a frame's first word and copies as words arrive, and a decision table: 3 idle cycles a frame at every point | stream block | 1,038 (-2.7%) | 2,421 (-0.1%) |
| B, rest | `pre`'s `COPY` loop entry: 6 cycles from a tag to the frame's first word at RTL, 3 in pysim (row 20) | the example | **1,046 (-2.0%)** | **2,424 (exact)** |

Untimed at the end: 1,035 (-3.0%) and 2,418 (-0.2%).

**Two right fixes moved pysim away first.** Stage 8's commit-on-a-first-word, before this plan, made two blocks
more accurate and took the bank from 9% to 16% short (`test`) and 20% to 26% (`long`): the arbiter's
store-and-forward had been hiding a larger gap (row 10). Then A, measured exact against every driver run,
overshot by 5-15%, and by 17-20% after C and D: the rule counted a gap as its source made it, which is right for a
source the FFT stalls, and in the bank charged the loop's own back-up a second time (row 18). An end-cycle
number alone would have rejected both fixes; the attribution showed each cause's share move as predicted while
another one surfaced.

At the end (every stream block timed; the gap now 21 and 0):

| cause | `test` | `long` |
|---|---|---|
| A, A2 | 0, +3 | 0, +1 |
| B | 0 | 0 |
| C | +16 | 0 |
| D | +3 | 0 |
| E, F | +2, -2 | 0, 0 |
| G, H | +1, -2 | +1, -2 |
| **the gap** | **21** | **0** |

## The gate and its tolerance

`tests/examples/test_os_filterbank.py::test_pysim_timing_is_within_tolerance_of_the_rtl`, a fast test with no
vendor tools: the bank in pysim on each scenario, every stream block timed on the packaged platform; each
channel's last status -- its burst's last beat -- against the cycle `test_rtl_timing_is_exact` pins at RTL, in
either direction. **Tolerance: 3% on `test`, 1% on `long`** (the user, 2026-11-22).

| scenario, channel | RTL | pysim | error | pysim untimed |
|---|---|---|---|---|
| `test`, 0 | 821 | 799 | -22 (-2.68%) | 793 (-3.41%) |
| `test`, 1 | 1,067 | 1,044 | -23 (-2.16%) | 1,033 (-3.19%) |
| `test`, 2 | 888 | 886 | -2 (-0.23%) | 880 (-0.90%) |
| `long`, 0 | 2,349 | 2,334 | -15 (-0.64%) | 2,328 (-0.89%) |
| `long`, 1 | 2,371 | 2,358 | -13 (-0.55%) | 2,352 (-0.80%) |
| `long`, 2 | 2,424 | 2,422 | -2 (-0.08%) | 2,416 (-0.33%) |

Mean error 1.05% (`dse_cg`'s whole system reached 1.44%). pysim is short on every channel; the untimed bank
fails `test`'s bound, so dropping the stream blocks' tables fails the gate.

## What it cost in speed

The bank's pysim, Stage 7 (`8bd6f12`, before Stage 8 and this plan) against this plan's end (`f2704b00`):
`examples/os_filterbank/speed.py compare`, the same script on both trees extracted from git, the same idle
machine (8 cores; Python 3.12, SimPy 4.1.2), 5 rounds of fresh processes alternating the trees, 5 runs a
process; the best of the 25 runs, timed around `sysm.run()` alone. Timed: the blocks each tree can time (the
arbiter at Stage 7; the arbiter and the demuxes at the end).

| scenario | mode | wall, Stage 7 -> end | SimPy events | end cycle (RTL 1,067 / 2,424) |
|---|---|---|---|---|
| `test` | untimed | 130.2 -> 134.1 ms (+3.0%) | 2,882 -> 3,213 (+11.5%) | 969 -> 1,035 |
| `test` | timed | 131.6 -> 136.2 ms (+3.5%) | 2,914 -> 3,226 (+10.7%) | 973 -> 1,046 |
| `long` | untimed | 355.2 -> 359.3 ms (+1.1%) | 7,615 -> 8,555 (+12.3%) | 1,934 -> 2,418 |
| `long` | timed | 354.2 -> 365.5 ms (+3.2%) | 7,698 -> 8,585 (+11.5%) | 1,944 -> 2,424 |

Both trees move the same data: 6,205 words in 661 bursts (`test`), 17,067 in 1,794 (`long`), on every link
of the bank and inside the FFT. So **0.50-0.52 events a word at the end, against 0.45-0.47** -- the
accuracy came from cycle counts and begun bursts (about half an event more a burst), not from word-level
events: one event a word on the links alone would be twice today's count. `long`'s simulated time grew 25%
(1,944 -> 2,424 cycles) for 3% more wall time.

## What remains

- **Row 21, open:** `post` begins its last inverse frame on `test` 38 cycles after the FFT's first word of
  it at RTL, 24 in pysim -- 14 of the 23 cycles at `test`'s end event (cause C). Not yet explained; none of
  it on `long`.
- **The small terms** on `test`: `front`'s block read (D, +3), the FFT's latency (A2, +3, within its
  documented 5), the arbiter (+2), the boundary (+1), the demuxes (-2).
- **A source the FFT stalls is early in pysim** (row 18): pysim's links move a burst at a time and do not
  stall a source word by word, so a testbench driver with random gaps runs 6-8% short of the RTL; the bank's
  `pre` is not such a source. Word-by-word back-pressure was rejected (2026-11-22): too slow for pysim. A
  named limit, pinned in `tests/dsp/ssr_fft/test_hw.py`.
- **`L = 16`** (row 19): no rate lost after a gap, but an arrival-phase jitter of up to 3 cycles a frame,
  stated and not modelled.
- **The hardware itself:** the FFT at half rate after a gap, and `pre`'s cycle between frames (rows 11, 13).
  When fixed, pysim models the fixed hardware and this gate re-measures.
- **One design, two scenarios.** The lessons are written for the next composite as a checklist:
  [Timing a composite](../../guide/timing_model/composites.md).

## Reproduce

```bash
python -m examples.os_filterbank.fidelity --rtl --timeline --attribute     # traced RTL, timelines, attribution
pytest tests/examples/test_os_filterbank.py -k pysim_timing                # the gate
mkdir -p /tmp/stage7 && git archive 8bd6f12 | tar -x -C /tmp/stage7       # a tree to compare against
python examples/os_filterbank/speed.py compare stage7=/tmp/stage7 head=.    # the speed table
```

## See also

- [Timing fidelity](index.md) -- the method and the ledger (rows 10-21 are this page's).
- [Timing fidelity: the stream blocks](stream_blocks.md) -- the arbiter's, dispatcher's and demux's tables.
- `docs/guide/dsp/ssr_fft/timing.md` -- the FFT's interval after a gap, its tables and its limit.
