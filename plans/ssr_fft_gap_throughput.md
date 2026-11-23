# Plan: SsrFft's throughput after a gap -- a future optimization

**Status: IN PROGRESS 2026-11-23 -- fix 1 started (the user), stopped for the user at Step 2.** `pre`'s HLS
body and its model are changed (Section "Fix 1: the record"); pysim, as it stands, cannot show the fix, because
its `SsrFft` never back-pressures `pre` -- so its prediction (no gain) is not one to trust, and the gate now
fails on `test` channel 0 (-3.05%, against today's RTL of the old body). A decision for the user, below
("Where fix 1 stopped"). Branch `claude/vibrant-meitner-3dlgxu`, from PR #340's `ccr-62d70dbe-679k63`.
Found by
`plans/osfb_pysim_fidelity.md` (cause A, Sections 5-6 of its record); the pysim *model* of today's behaviour
is that plan's work. This plan is the *hardware* (or design-pattern) fix.

## The finding (lab machine, Vitis / xsim 2025.1, `SsrFft` alone, every frame bit-exact)

At `L >= 64` the RTL's interval between frames, for a gap of `g` idle cycles before a frame, is piecewise:
`L/R` at `g = 0` (back to back, as designed), and `max(2L/R, L/R + g)` for `g >= 1` -- so **any gap from 1 to
`L/R` costs a whole frame time** (the max form alone would wrongly give `2L/R` at `g = 0`). Exact and deterministic (constant and random gaps, `L = 64, 256, 1024`). Set by the last transposer
commutator (`tp{S-2}`, block size `L/16`): its input frame stretches to `2L/R - L/16` cycles, and every
stream after it carries a frame every `2L/R`. At `L = 16` there is no throughput loss (an arrival-phase
latency jitter of 40-43 cycles instead). `L = 4096` not yet measured.

*Update (lab machine, 2026-11-22, `plans/osfb_pysim_fidelity.md` Section 6):* `L = 4096` is measured since -- the
same rule, exact on every constant gap and 23 of 23 random intervals, set by `tp4`. And at `L = 16` constant
gaps of 1-3 cycles do cost throughput, a little: the RTL alternates intervals of 8 and 4 at a 1-cycle gap
(68 cycles over 11 frames against 55 for `L/R + g`); on random gaps its total is `L/R + g`'s.

**What it costs at `L = 1024`, `R = 4`** (a frame is 256 cycles, 1.02 µs at 250 MHz):

| gap before a frame | RTL interval | ideal `L/R + g` | throughput lost |
|---|---|---|---|
| 0 | 256 | 256 | none |
| 1 | 512 | 257 | ~50% |
| 16 | 512 | 272 | 47% |
| 128 | 512 | 384 | 25% |
| >= 256 | 256 + g | 256 + g | none |

Random gaps (a third 0, the rest uniform 1..512): RTL 9,587 cycles against the ideal 8,299, +15.5%. In a
shared-FFT loop whose frames arrive a cycle or so apart -- `examples/os_filterbank`, where `pre` leaves at
least a cycle -- the FFT runs at **half its rated throughput**.

## Why it matters

A shared FFT is the pattern Waveflow teaches (`docs/guide/systems/shared_kernels.md`, `os_filterbank`), and a
shared FFT seldom gets its frames back to back. The FFT's documented interval, "`L/R`, by construction"
(`docs/guide/dsp/ssr_fft/timing.md`), holds only back to back. The FFT-sharing evaluation problems in
`plans/dse_matrix_sched.md` Section 11 (spectrum sensing, delay-Doppler) depend on it.

## The mechanism (read from `ssr_fft_commutator_task`, `waveflow/dsp/ssr_fft/src/ssr_fft_tasks.h`)

A commutator ticks in **groups of `R*D` ticks** and decides each group only at its first tick: **data** if a
word is waiting; a **bubble group** if the last group was data ("one is always enough to push a group's tail
out"); idle otherwise. A bubble group runs all `R*D` ticks even if data arrives meanwhile, because the switch's
slot is a function of the tick count, so a frame can start only at a group boundary. The flush is deliberate
-- an isolated frame must leave without a next one pushing it (the shared-kernel return-path rule) -- but it
is a whole group long. For the **last transposer commutator** `D = L/16`, so a group is `R*D = L/R` ticks,
**exactly a frame**: a frame missing the boundary by one cycle finds a frame-long bubble group committed, hence
`2L/R`. Earlier commutators have smaller `D` (shorter bubble groups, smaller stretch, as traced); at `L = 16`
the only one has `D = 1` (a 4-tick group: the arrival-phase jitter). Inside a data group a missing word only
stalls (no tick), so gaps *within* a frame cost only the gap.

## Fixes, ranked (proposed 2026-11-22 by the planning session; to be confirmed when started)

1. **Feed the FFT back to back -- no FFT change.** Throughput is lost only when the FFT is the bottleneck,
   and then frames are waiting: the gap comes from the stage in front. In `os_filterbank`, `pre` spends a cycle
   or more a frame deciding (reading `m_sel`, writing `s_dir` and the tag). Overlap that with the end of the
   previous frame and the gap is 0 whenever a frame waits (`docs/guide/vectorization/hls/loop_optimization.md`:
   "do not decide, compute and commit in one iteration"). When nothing waits the FFT is not the bottleneck, and
   a gap costs only latency (up to `L/R`). Cheapest, probably sufficient; becomes a guide rule: whatever feeds
   `SsrFft` keeps it back to back when a frame is ready.
2. **Shorten the flush (HLS).** The tail is out after `(R-1)*D` bubble ticks, not `R*D`; everything inside is a
   bubble then, so the commutator can return to a group boundary and take a waiting frame. Worst case at
   `L = 1024`: 448 cycles instead of 512 (~12%). Keep it a fixed tick count: a count of samples inside once
   cost 1.3 ns of slack at 250 MHz (the body's comment). The pysim cycle reference (`cycle_ref.CommutatorTask`)
   changes with it, statement for statement.
3. **Defer the flush with a timeout `W`** (a build parameter): at a boundary with nothing waiting, wait up to `W`
   cycles before committing to a bubble group. Gaps up to `W` cost only the gap, but an isolated frame leaves `W`
   later -- `W` added to every feedback pass in a loop -- and gaps just over `W` still pay the full group. For a
   design that knows its gap pattern, not a default.
4. **Start a frame at any tick** (a rotation offset tracked through the transpose): a redesign, and the road
   to the vendor core's arrival-phase jitter. Not recommended.

Do 1 first; 2 if a design cannot keep its feeder back to back.

## Two fixes, to compare (as first written; superseded by the ranking above)

1. **The design pattern (no FFT change).** The loss needs a gap; a producer that holds the next frame whole
   and starts it the cycle after the previous one ends gives `g = 0`. Check whether `os_filterbank`'s `pre`
   (and the arbiter in front of it) can feed the FFT back to back when a frame is waiting, and what that
   costs (a frame of buffering, a cycle of decision). If it works, it is a rule for the shared-kernel guide:
   "feed a streaming unit back to back".
2. **The FFT's HLS.** Make `tp{S-2}` (and any other commutator with the same behaviour) accept a frame that
   follows a gap without losing a frame time -- the interval `L/R + g`. Read the commutator's body and its
   csynth schedule first: why a gap makes it wait for a full window. The usual cause is a loop that restarts
   per frame and only re-enters at its window boundary; the fix keeps the window running across the gap.
   Then: the gap sweep at every `L` (16 to 4096), the back-to-back interval and latency pins unchanged, every
   frame bit-exact, resources and timing at 250 MHz on `xczu48dr` (the RFSoC probe), the pysim model and
   `timing.md` updated to the fixed hardware.

Do 1 first (cheaper, and it may be enough); do 2 if a design cannot keep its producer back to back.

## Done when

- The chosen fix measured at RTL: `os_filterbank`'s FFT busy fraction and end cycles before and after (today
  39-48% busy, ends at 1,067 / 2,424); the gap sweep at every `L`.
- The guide (`ssr_fft/timing.md`, `shared_kernels.md`) and the model match the hardware; gates pass.

## Fix 1: progress log

| date | step | what happened |
|---|---|---|
| 2026-11-23 | -- | Started by the user: fix 1, no FFT code; design and evaluate in pysim first; stop before any change to `SsrFft`, the stream blocks or other framework code, before re-pinning an RTL number, and before trading accuracy for speed. Branch `claude/vibrant-meitner-3dlgxu` from `ccr-62d70dbe-679k63` (PR #340, not yet merged). |
| 2026-11-23 | design | **Where the tag's cycle goes decides what it costs.** `pre` reads a frame's tag and its `L/R` words off one stream (`s_frm`), so a frame is `L/R + 1` reads and one cycle a frame carries no word to the FFT. Between frames it costs a frame-time (the rule). Inside a frame it costs one cycle at some positions and a frame at others: `examples/os_filterbank/hole_cost.py`, a cycle model of the chain from `cycle_ref.CommutatorTask` (checked against the RTL's gap sweep: exact at gaps 3-24, optimistic at 1-2). A hole between words 0 and 1 costs one cycle at `L = 64, 256, 1024`; one before the last word -- the obvious "hold the last word for the next tag" -- costs a frame. |
| 2026-11-23 | 1 | `src/os_pre_task.h`: one `while (1)` loop at II=1 that never exits (the FFT's own task shape; `rf_samp_buf_player_task.h`'s measured licence), `empty()` tested before every read; a one-word skid -- every word but a frame's first leaves one cycle after it is read, the first bypasses it, and the iteration that reads the next tag writes the previous frame's last word. `OsPre.run_iter` charges the same from the loop's structure (`STAGE_CYC["pre_skid"] = 1`; `pre_copy` is gone with the loop it timed). Tests: `test_pre_feeds_the_fft_back_to_back_when_frames_wait` (pysim, `pre` alone, every frame waiting: back to back, `NW + 1` cycles a frame) and `test_pre_body_moves_words_as_its_model` (the body's loop under g++ with stand-in headers, `tests/examples/os_filterbank_cpp/`: every word, tag and direction in order, on the model's cycles; an isolated frame's last word not held). No vendor tools on this machine: no csim, no csynth. |
| 2026-11-23 | 2 | **pysim's prediction (every stream block timed), against today's RTL: no gain.** The FFT still a frame every 32 cycles, no gap of 0 into it; end cycles 1,046 -> 1,038 (`test`) and 2,424 -> 2,421 (`long`). Why: pysim's `SsrFft` takes frames into its tasks (up to `MAX_INFLIGHT = 4` each) without stalling `pre`, so in pysim no frame ever waits at `pre`, and its frames reach the FFT at the arbiter's pace (21 cycles: 17 words, 4 idle) -- a gap of 4 every frame. At RTL the FFT stalls `pre` through FIFOs two to four words deep, which is what makes frames wait there, and what fix 1 uses. So this prediction is not one to trust. The gate fails on `test` channel 0, -3.05% (796 against 821): the new body's head is 4 cycles shorter than the old one's 6. **Stopped for the user** ("Where fix 1 stopped"). |

## Fix 1: the record (2026-11-23)

### The body (`examples/os_filterbank/src/os_pre_task.h`)

One iteration a cycle, forever (`while (1)`, `PIPELINE II=1`; the step is `os_pre_body::step`, its own
inline function so the g++ check can run it a cycle at a time):

- **the head**, when no frame is being read: if a word is held, write it (the previous frame's last); if
  `s_sel` and `s_frm` both hold a word, read the index and the tag, write the tag, keep the direction;
- **a frame's first word**: read and written at once, the direction beside it (the FFT's `in_reg` reads its
  direction at a frame's first word);
- **every other word**: written one iteration after it is read (`held`). Word 1's iteration writes nothing:
  that is the tag's cycle, between words 0 and 1.

So with frames waiting, a frame is `L/R` words in `L/R + 1` cycles and the next frame's first word follows
the previous one's last on the next cycle. With nothing waiting, the last word leaves the cycle after it was
read, alone. The decision (both heads present) and its copies are one iteration; the direction is computed
there and written the next, with the first word (the timing rule). Statics under `#pragma HLS reset`;
nothing is written that was not read. Not synthesized here (no Vitis on this machine): its II, its clock at
250 MHz and its resources are the lab's to check.

**Why the hole goes between words 0 and 1** (`python -m examples.os_filterbank.hole_cost`; intervals of a
run of frames, a 1-cycle hole before word `p` of every frame, otherwise back to back):

| L | `p = 1` | `p = 2` | `p = L/8` | `p = L/R - 1` |
|---|---|---|---|---|
| 64 | 17 every frame | 17 | a 32 among 16s | a 32 every 6 frames |
| 256 | 65 | 65 | 128 every third | 128 every 7 |
| 1024 | 257 | 257 | 512 every third | 512 every 8 |

A hole moves through each commutator by `(R-1)*D` words; reaching a frame-sized group's boundary (`tp{S-2}`,
`cm0`, `rc`) it costs a frame. The model is optimistic at gaps of 1-2 cycles between frames (it absorbs one
frame in two or three that the RTL does not), so a cost it reports is a lower bound, and a cost of one cycle
is the RTL's to confirm.

### `pre`'s model (`OsPre.run_iter`)

A frame a firing, at burst granularity, as the loop: the head (`_head`) once both are here and the previous
frame's words are read; the first word the next iteration (`begin_read`, then `m_dir.begin` and `m_x.begin`
together); the words copied as they arrive, read a word a cycle (`end_read(charge_beats=True)`); the next
iteration takes the next frame's head if it is already here (`_head(wait=False)`, both streams' buffers
tested, as the body tests `empty()`), and the burst ends `1 + STAGE_CYC["pre_skid"]` cycles after the last
word's read. Its beats against the body's, `pre` alone with every frame waiting: the same cycles
(`test_pre_body_moves_words_as_its_model`). In the bank, cut-through: the first word one beat after it
reaches `pre`, the last two (the old body: 5 and 5, the tag to the first word 6).

### The pysim prediction (pysim, not RTL; every stream block timed)

| | `test`: today's RTL | pysim, old `pre` | pysim, new `pre` | `long`: today's RTL | pysim, old | pysim, new |
|---|---|---|---|---|---|---|
| end cycle | 1,067 | 1,046 | 1,038 | 2,424 | 2,424 | 2,421 |
| last status, ch 0 / 1 / 2 | 821 / 1,067 / 888 | 799 / 1,044 / 886 | 796 / 1,036 / 883 | 2,349 / 2,371 / 2,424 | 2,334 / 2,358 / 2,422 | 2,331 / 2,355 / 2,419 |
| gaps of 0 into the FFT | -- (a cycle or more) | 0 of 25 | 0 of 25 | -- | 0 of 71 | 0 of 71 |
| gaps into the FFT, min / median | -- | 5 / 17 | 4 / 16 | -- | 5 / 17 | 4 / 16 |
| FFT interval | 32 | 32 | 32 | 32 | 32 | 32 |
| FFT busy (`L/R` x frames / end) | 39% | 40% | 40% | 48% | 48% | 48% |
| `post` busy (pysim: the frame's first word in to its last out) | -- | 70% | 70% | 91% (RTL) | 83% | 83% |

The bottleneck in pysim stays the FFT at half rate. **This is pysim's limit, not a prediction of the
hardware** (next section).

### Where fix 1 stopped (2026-11-23): a decision for the user

**pysim's `SsrFft` does not stall its source, so in pysim fix 1 has nothing to act on.** Each of its tasks
takes up to `MAX_INFLIGHT = 4` frames before it writes one (`waveflow/dsp/ssr_fft/hw.py`), so `pre`'s
writes into the FFT never wait: in the bank `pre` copies each frame through as the arbiter delivers it, a
frame every 21 cycles, and the FFT's rule (`GapRhythm`, counted from the arrivals) sees a gap of 4 every
frame. At RTL the FFT's input FIFOs are two to four words deep: after a gapped frame the FFT holds `pre`
(the input stretches to 28-31 cycles a frame, Section 5 of `plans/osfb_pysim_fidelity.md`), `frm` fills,
and the next frame is waiting when the last word goes in -- the case fix 1 is for. So the RTL should run
faster than pysim says; by how much pysim cannot say. Bounds: the arbiter feeds a frame every 21 cycles at
best, `pre` every 17, and `post` takes about 30 a frame on average (busy 91% of `long` at RTL); so at most
`post`'s limit, roughly `2,424 x 30/32`, a few percent on `long` (`post` becomes the bottleneck).

This is the "source the FFT stalls" limit `GapRhythm` names (word-by-word back-pressure rejected,
2026-11-22), now on the bank's critical path. Options:

- **(a) Model `SsrFft`'s input back-pressure at frame granularity** (framework: `hw.py`, the `SsrTask` slots
  in front of `tp{S-2}` and the boundary of the flat pysim): the FFT takes a frame's words no earlier than
  its RTL would (its input side holds a few words, not four frames), and a gap is the idle time at its
  input as it takes the words. No event per word -- one per burst, as now. Then re-measure the driver pins
  (`tests/dsp/ssr_fft/test_hw.py`, the 6-8% named limit may close) and the bank with the old `pre` (the gate
  must still pass) before the new `pre`'s prediction means anything. `hole_cost.py` is a check for the rule.
- **(b) Measure first**: take the body to the lab as it is (csynth: II=1, 250 MHz; the bank's two gates;
  the FFT's gaps from the trace), keep the old `pre` model's prediction out of the record, and model what
  the RTL shows -- the gate re-pinned with the new RTL (the user's approval, to re-pin).
- **(c) Both**: (b) first, as it settles whether the fix works at all; then (a), calibrated on (b)'s trace.

**The gate meanwhile** fails on `test` channel 0 (-3.05% against 3%; `long` passes, worst -0.77%): it
compares the new body's pysim with the old body's RTL. It is left failing rather than loosened or re-pinned
(both the user's decision).

### Hand-off to the lab (draft, for after the decision)

On the lab machine (Vitis / Vivado / xsim 2025.1, xczu48dr), on this branch:

1. `pytest tests/examples/test_os_filterbank.py -m xsi` -- csynth of the bank (check `os_pre_task`'s loop at
   II=1 and the clock at 250 MHz in its report), the recordings against pysim (`compare`, both scenarios);
   `test_rtl_timing_is_exact` will fail with the new cycles: record them.
2. `python -m examples.os_filterbank.fidelity --rtl --timeline --attribute`: the FFT's gaps (how many 0), its
   interval and busy fraction, `post`'s, the end cycles -- against the pysim prediction above.
3. With the user's approval: re-pin `LAST_STATUS` (the RTL gate and the pysim gate's reference together).
