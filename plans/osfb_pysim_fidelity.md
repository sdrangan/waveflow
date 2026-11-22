# Plan: why os_filterbank's pysim runs 17-27% short of its RTL, and a gate so it cannot drift again

**Status: ACCEPTED 2026-11-22 (the user), for a lab machine with Vitis / Vivado 2025.1. Steps 1-3 DONE
(`examples/os_filterbank/fidelity.py`; Section 5). Step 4: cause A modelled in `SsrFft` (Section 6, exact
against every driver measurement at `L = 64 .. 4096`), B, C, D done in the example's stages, open question 2
re-measured and the page corrected; STOPPED for the user on two decisions (Section 8): how `SsrFft`'s rule
counts a gap (the bank lands at -2.6% / +0.0% one way, +19.9% / +17.2% the other), and E, F, H (framework).**
Written by the cloud planning session from `plans/stream_dispatch.md` Stage 8's record (commits `4552413`,
`03a9b4f`). Branch `ccr-62d70dbe-679k63`; pull first, commit and push after each step.

**The user's priority (2026-11-22): the timing model's accuracy is what matters.** Speed is secondary -- the
user accepted Stage 8's ~25% pysim slowdown for fidelity. So no step here trades accuracy for speed, and no
fix absorbs cycles it cannot explain.

## 1. What was found

`examples/os_filterbank` (`docs/examples/os_filterbank/index.md`) is the curated shared-kernel example: three
channels, one `SsrFft(direction="runtime")` used forward then inverse per block, a `StreamArbiter` (feedback on
input 0, priority), `pre` (direction from `m_sel`, the tag), `post` (x H_c, the feedback frame, answers,
credits), `StreamDemux`es back to each channel, per-channel credits. Its end cycle (the last word on any
output; RTL on xczu48dr at 250 MHz, Vitis / xsim 2025.1):

| scenario | RTL | untimed pysim, Stage 7 | untimed pysim, Stage 8 | arbiter timed, Stage 7 | arbiter timed, Stage 8 |
|---|---|---|---|---|---|
| `test` (13 blocks, 26 FFT frames) | 1,067 | 969 (-9.2%) | 885 (-17.1%) | 973 (-8.8%) | 901 (-15.6%) |
| `long` (36 blocks, 72 FFT frames) | 2,424 | 1,934 (-20.2%) | 1,766 (-27.1%) | 1,944 (-19.8%) | 1,800 (-25.7%) |

- **Stage 8 was right:** the arbiter now copies a frame as its words arrive, as the HLS body does, instead of
  store-and-forward (13-17 cycles a frame that the RTL does not spend). Removing that wrong cost **exposed**
  a larger gap it had been hiding: after it, pysim is still **166 cycles short on `test` and 624 on `long`**
  with the arbiter's decision cycles timed. Keep Stage 8; do not put the wrong cost back.
- **The gap grows with load** (-17% to -27%; 166 to 624 cycles for 2.8x the frames, so faster than the frame
  count): a missing contention or backpressure effect, not a fixed latency per frame.
- **Nothing asserts pysim's timing here.** The gates compare the recordings' *words* (`compare`) and pin the
  RTL's cycles (`test_rtl_timing_is_exact`); no test compares pysim's cycles to the RTL's. That is how -9%
  and then -27% went unnoticed.
- **Why it matters beyond this example.** Every library block in the loop is calibrated alone (`SsrFft`: first
  frame within 5 cycles of RTL, interval exact; the arbiter's decision table), yet the loop composed from them
  is 17-27% off. `plans/dse_matrix_sched.md`'s engine is also a loop (dispatcher, CG units, arbiter, credits),
  and `plans/dse_arch.md`'s key claim is that block-level calibration transfers to unseen compositions. This
  example is the first counter-evidence; find out why.

## 2. Hypotheses (to test, not to assume)

- **H1. The example's own stages are untimed in pysim.** `front`, `pre` and `post` are example kernels with
  HLS bodies (`examples/os_filterbank/src/os_{front,pre,post}_task.h`); their pysim `run_iter`s may charge
  only the links' word rate. The page says `post` takes **43 cycles a frame against the FFT's 16** at RTL
  (store and forward, serial) and keeps the FFT 39-48% busy -- if pysim's `post` is faster, the loop is
  faster, and more so under load. **The prime suspect.**
- **H2. `SsrFft` in a loop.** Calibrated on back-to-back and isolated frames (`docs/guide/dsp/ssr_fft/timing.md`),
  but here frames arrive in an irregular mix of directions with gaps; the per-frame direction word (`s_dir`)
  and the flush of an isolated frame may cost cycles the model does not charge.
- **H3. The arbiter's table at the wrong width and frame.** Measured at 32 bits, 1-burst frames; this arbiter
  runs 128 bits, 2-burst frames, where Stage 7's spot check found every gap a cycle longer: about 26 and 72
  cycles -- part of the gap at most.
- **H4. Costs inside a composite** (`plans/stream_dispatch.md` open question 5): FIFO links between tasks
  instead of top-level ports; the demuxes' firings (untimed in pysim); link depths and backpressure.
- **H5. The boundary.** The testbench drivers (the RTL `AxisMaster` against pysim's driver), the `s_coef` load
  before the first block, the first job's start.

## 3. Steps

### Step 1: reproduce, with one script

`examples/os_filterbank/fidelity.py` (new): runs the bank on both scenarios, pysim untimed and with the
arbiter on the platform, and reads the RTL's end cycle and each channel's last status from the system flow's
traces. Prints the table of Section 1. **Done when** it reproduces 1,067 / 2,424 at RTL and 885 / 1,766
untimed.

### Step 2: a per-frame timeline, both backends

For every FFT frame, keyed by its tag (channel, job, block, pass): the cycle its first and last word cross
each link -- `front -> arb`, `fb -> arb`, `arb -> pre`, `pre -> fft`, `fft -> post`, `post -> fb`,
`post -> dmx_*`. Also each task's firing windows.
- **RTL:** a traced `system_xsi` run (the flow's `trace=True`) and `waveflow.utils.trace` (`load_trace`,
  `component_firings`; `docs/guide/timing/vcd.md`, `docs/guide/systems/rtl.md`).
- **pysim:** the same events from the simulation (each link's burst arrival times; the stream-block fixture
  `waveflow/calib/fixtures/stream_blocks.py` already records pysim firings and is the pattern).
- Write both as one CSV per backend in the same schema (a row a frame and link: tag, link, first, last).

**Done when** both CSVs exist for `test` and `long`, every frame present in both.

### Step 3: attribute the missing cycles

Align the two timelines frame by frame. For each stage, split the difference into **service** (the frame's
time through the stage, RTL against pysim) and **waiting** (time queued before it). Find the first stage where
pysim's per-frame times diverge, and attribute the 166 and 624 cycles to stages and causes (H1-H5 or others).

**Done when** a table attributes the end-cycle gap to named causes, summing to within 10% of it on both
scenarios. **If it does not sum, stop and report** -- do not fix what is not explained.

### Step 4: fix each cause where it belongs, one at a time

- **An example stage's cost** (H1): a timing model on that stage, the framework's way
  (`FreeRunMod.add_timing_model` / `timed_delay`, calibrated on the platform as `waveflow/streams/timing.py`
  and `waveflow/vitis_l1/timing.py` do), or its `run_iter` charging the cycles its HLS body spends, derived
  from the body's loop structure and checked at RTL -- whichever the existing examples use; read
  `docs/guide/timing_model/` and `docs/guide/calib/` first.
- **A library block's cost** (H2-H4): in that block's model or calibration.
- **Never** a constant added at the end, or anywhere, to absorb cycles no cause explains.
- After each fix: the Step 1 table and the Step 3 attribution again, before and after, in the progress log.

**Stop and ask the user before** changing framework code beyond a block's own timing model
(`waveflow/hw/interface.py`, `hw_freerun.py`, `SsrFft`'s model, the calibration machinery), and if any gate's
recorded number moves.

### Step 5: the gate

- **A fast test** (no vendor tools): pysim's cycle for each channel's last status against the RTL cycles
  `test_rtl_timing_is_exact` already pins (`test`: 821 / 1,067 / 888; `long`: 2,349 / 2,371 / 2,424). It
  fails if pysim drifts, in either direction, beyond the tolerance.
- **Tolerance:** propose one from Step 4's result (dse_cg's whole system reached 1.44% mean cycle error;
  `SsrFft`'s first frame is within 5 cycles). **Ask the user to confirm it** before committing the gate.
- If the gap cannot be closed to a tolerance the user accepts, the gate records the gap that remains, so it
  cannot grow unnoticed, and the remainder is written up as an open question.

### Step 6: record and generalise

- The progress log here; `docs/examples/os_filterbank/index.md` (a pysim-against-RTL row next to the RTL one);
  the shared-kernel guide (`docs/guide/systems/shared_kernels.md`) if a cause is general to loops.
- **Write the general lesson**: what a composite's pysim needs beyond calibrated blocks (e.g., every task in
  the loop timed, not only the library's), so the next design checks it. Put it where a design reads it
  (`docs/guide/systems/` or `docs/guide/timing_model/`), and note it in `plans/dse_matrix_sched.md`
  (precondition 4, the engine) and `plans/dse_arch.md` (the composition-transfer claim).
- Commit, push, stop and report: the attribution table, each fix with its before and after, the gate and its
  tolerance, what remains.

## 4. Progress log

| date | step | what happened |
|---|---|---|
| 2026-11-22 | -- | Plan written by the cloud planning session from Stage 8's record; accepted by the user. |
| 2026-11-22 | 1 | `examples/os_filterbank/fidelity.py` (lab machine, Vitis / xsim 2025.1; the bank's system flow traced in `work/osfb_fidelity/`). Reproduces Section 1: RTL 1,067 / 2,424 (each channel's last status 821 / 1,067 / 888 and 2,349 / 2,371 / 2,424, as `test_rtl_timing_is_exact` pins); untimed pysim 885 / 1,766; the arbiter timed 901 / 1,800. |
| 2026-11-22 | 2 | `fidelity.py --timeline`: a CSV per backend and scenario, a row a frame and link (`ch, tx_id, blk, npass, link, first, last`, write-side beats), every link of the bank and its outputs; the same 246 (`test`) and 664 (`long`) frame-links at RTL, untimed and timed pysim, each once. RTL beats are put on the recordings' origin: the waveform runs 15 cycles ahead on every output port, both scenarios. Tooling gaps met (worked around in the script, not the framework; Section 5). |
| 2026-11-22 | 3 | `fidelity.py --attribute`: the gap along the RTL's critical path, every hop charged to a cause; the causes sum to the gap exactly (166 and 624 with the arbiter timed, 182 and 658 untimed). **Cause A, `SsrFft`'s interval after a gap, is 92-103% of it**: `SsrFft` alone at RTL, frames a gap `g` apart, runs a frame every 16 cycles at `g = 0` and every **32** at every `g` from 1 to 16 (40 at 24) -- `max(2L/R, L/R + g)` -- where its pysim runs `L/R + g`; the same with a fixed direction (`fidelity.py --fft-gaps runtime fixed`; every frame bit-exact). In the bank `pre` leaves a cycle or more between frames, so the RTL's FFT runs at 32 throughout: 72 frames x 32 plus the fill is the `long` scenario's 2,424. Section 5 has the table and the rest (B-H, each under 25%). **Stopped before Step 4**: cause A's fix is in `SsrFft`'s model. |
| 2026-11-22 | 4 (A, rule first) | The user approved cause A's fix, the rule confirmed first (Section 6). `fidelity.py --fft-gaps 64 16 256 1024` (`SsrFft` alone, constant and random gaps, a traced run; Vitis / xsim 2025.1; every frame bit-exact): **exact and deterministic at `L = 64, 256, 1024`** -- every constant gap and 23 of 23 random intervals follow `max(2L/R, L/R + g)` -- set by the last transposer commutator (`tp1`, `tp2`, `tp3`). **At `L = 16` the intervals spread**: 16 of 23 random intervals, off by -4 to -1; there the FFT loses no throughput (its intervals sum to `L/R + g`, which today's pysim matches in total) but each frame's latency moves 40-43 cycles with the gaps before and after it, an arrival-phase effect. **Stopped before modelling**, as the approval says, for the user (proposal in Section 6). Two open questions noted (Section 7). |
| 2026-11-22 | 4 (A, the model) | The user approved the rule where `L >= 64`, `L = 16` as it is. **`L = 4096` measured** (`fidelity.py --fft-gaps 4096`, Vitis / xsim 2025.1): every constant gap and 23 of 23 random intervals follow the rule, set by `tp4`. **The model** (`waveflow/dsp/ssr_fft/hw.py`, `GapRhythm`): `tp{S-2}` at `L >= 64`, and the flat pysim at its boundary (its pin against the chain holds); pysim now equals the RTL on **every** measured interval at `L = 64, 256, 1024, 4096` (constant and random gaps). `tests/dsp/ssr_fft/test_hw.py`: `test_composite_pysim_keeps_the_rtl_interval_after_a_gap` (64, 256, 1024, 4096) and `test_composite_pysim_at_l16_is_within_three_cycles_a_frame`; the back-to-back pins did not move (138 FFT tests pass). **`L = 16` corrected**: within 3 cycles a frame and the same total on random gaps, but at constant gaps of 1-3 cycles the RTL falls up to 13 cycles behind over 11 frames (it alternates 8 and 4 at a 1-cycle gap) -- not "exact in total"; so stated in the model and `timing.md`, which now gives the rule, its tables, and no longer says "`L/R` by construction". |
| 2026-11-22 | 3 again (after A) | `fidelity.py --timeline --attribute`, the RTL unchanged. **End cycle, RTL / pysim before A / after A**: `test` 1,067 / 901 (-15.6%) / **1,228 (+15.1%)** with the arbiter timed, 885 (-17.1%) / 1,263 (+18.4%) untimed; `long` 2,424 / 1,800 (-25.7%) / **2,540 (+4.8%)** timed, 1,766 (-27.1%) / 2,621 (+8.1%) untimed. pysim now **overshoots**. Attribution after A (timed; sums exact): `test` (gap -161) A -48, A2 -119, B -39, C +21, D +15, E +5, F +4, G +12, H -12; `long` (gap -116) A -97, A2 +1, B -13, C -11, E +1, F +3, G +12, H -12. Why A now overshoots in the bank: the FFT's frames leave 32 cycles apart at RTL throughout `long`, but in pysim 33, 34 or 47 at times (24 of 71): the rule counts a gap as its source made it -- right for a source the FFT stalls (every driver measurement, exactly) -- and pysim's `pre` is neither stalled by the FFT (its links hold a whole frame, its tasks four) nor cut-through (cause B), so its own spacing, at times over 32, is charged in full. The diagnosis, to be tested by B: A's residual here comes from how pysim's `pre` feeds the FFT (B, and the back-pressure pysim lacks, H4), not from the FFT's rule. Fast suite: 6,094 passed, 9 failed (the known environment set). |
| 2026-11-22 | 4 (B) | `pre` copies the frame to the FFT as its words arrive (`copy_burst`, as the HLS `COPY` loop), no longer once the whole frame is in. End cycle with the arbiter timed, after A / after B: `test` 1,228 / 1,212 (+13.6%), `long` 2,540 / 2,524 (+4.1%). B's share: -39 -> +9 (`test`), -13 -> +3 (`long`). |
| 2026-11-22 | 4 (C, D) | `post` and `front` do what their bodies do (`os_filterbank.STAGE_CYC`, read off the traced RTL at each loop's least): `front` reads its block a word a cycle (`end_read(charge_beats=True)`), its Job header's `CLEAR` loop 11 cycles, its loops' exit and entry 1 and 2; `post` reads the FFT's frame as it arrives, pass 0 the `MUL` loop's drain 4 and the `FB` loop's entry 2, pass 1 the `Resp` first and the answer copied through, from the frame's first word plus 5. Checked: every `post` hop's least now the RTL's (y last -> feedback tag 5, tag -> words 3; y first -> answer 6, its end 2 after y's). G (the first frame's start) +12 -> +1, D +15 -> +3. End cycle (timed): `test` 1,279 (+19.9%), `long` 2,841 (+17.2%): the stages are right, and with `post` as slow as the RTL's the loop backs up in pysim and A's counting charges the back-up again (A -414 of -417 on `long`). Attribution (timed): `test` A -76, A2 -185, B +9, C +39, D +3, E +5, F +4, G +1, H -12 (gap -212); `long` A -414, A2 +1, B +3, C 0, E +1, F +3, G +1, H -12 (gap -417). |
| 2026-11-22 | 4 (A, counting) | **An experiment for the decision** (`fidelity.py --rule-variants`: patches the rule in its own process, the FFT flat): counting a gap from the previous frame's start (as built), the bank ends at 1,277 / 2,839 (+19.7% / +17.1%); from the arrivals (`max(arrival, prev + 2L/R)`), **1,039 / 2,425 (-2.6% / +0.0%)**; net of pysim's own stall, as built (pysim's `pre` is not stalled). The arrival counting misses the driver measurements, though: 17, 19, 19 of 23 random intervals at `L = 64, 256, 1024` (as built: 23 of 23). Why: at RTL a stalled driver offers its next frame only its gap after the FFT took its last word, and pysim's links (a whole frame each) do not stall it so; in the bank `pre`'s frames come when their data does. Section 8. |
| 2026-11-22 | open question 2 | Re-measured on the traced RTL (Vitis / xsim 2025.1): the FFT's frames leave 32 cycles apart throughout `long` (cause A: at most half busy; busy 48%), while `post` takes 39-40 cycles a forward frame and 19-22 an inverse one, about 30 on average, busy 91% of `long`. So the FFT's own half rate after a gap is why it is under half busy; `post` would bind next. `docs/examples/os_filterbank/index.md` corrected (it named `post` alone). |

## 5. Steps 1-3 record (2026-11-22, lab machine, Vitis / Vivado / xsim 2025.1)

**The attribution** (`python -m examples.os_filterbank.fidelity --attribute`; the arbiter timed on the platform;
cycles RTL minus pysim, charged along the RTL's critical path, which the hops' differences telescope along, so
they sum to the end-cycle gap exactly):

| cause | `test` (gap 166) | `long` (gap 624) |
|---|---|---|
| A. `SsrFft`'s interval after a gap: RTL `max(2L/R, L/R+g)`, pysim `L/R+g` | +157 (95%) | +643 (103%) |
| A2. `SsrFft`'s latency (within its documented 5 cycles) | +3 | +1 |
| B. `pre`: pysim waits for the whole frame, the RTL copies it through | -39 | -13 |
| C. `post`: its loops' overheads (`fb` 19 cycles for 17 words, 17 in pysim), pass 1 copied through at RTL | +21 | -11 |
| D. `front`: after a credit, the RTL reads the block's 12 words before the frame; pysim has it already | +15 | 0 |
| E. the arbiter's decision at 128 bits, 2-burst frames (its table: 32 bits, 1 burst; open question 7) | +5 | +1 |
| F. the demuxes' decision cycles (`StreamDemux` is untimed in pysim) | +4 | +3 |
| G. the boundary: the first frame starts at 25 at RTL, 13 in pysim | +12 | +12 |
| H. pysim's last word is another event than the RTL's (`dmx_ans` stores and forwards in pysim, so its last answer word leaves after its status) | -12 | -12 |
| **sum** | **166** | **624** |

Untimed (the arbiter on no platform): the same causes, A +167 of 182 and +675 of 658; E +11 and +3.

**Cause A, measured alone** (`fidelity.py --fft-gaps runtime fixed`: `SsrFft`, `L = 64`, ping-pong, 12 frames,
the FFT's own system flow; every frame bit-exact):

| gap between input frames (cycles) | 0 | 1 | 2 | 3 | 4 | 8 | 12 | 16 | 24 |
|---|---|---|---|---|---|---|---|---|---|
| RTL interval, runtime direction | 16 | 32 | 32 | 32 | 32 | 32 | 32 | 32 | 40 |
| RTL interval, fixed direction | 16 | 32 | | | 32 | | | 32 | |
| pysim interval (both) | 16 | 17 | 18 | 19 | 20 | 24 | 28 | 32 | 40 |
| RTL input frame accepted over (cycles) | 16 | 31 | 30 | 29 | 28 | 24 | 20 | 16 | 16 |

So any gap of 1 to `L/R` cycles costs the RTL a whole frame-time; `docs/guide/dsp/ssr_fft/timing.md` ("the
interval is `L/R`, by construction") was measured back to back and on isolated frames, both of which this
leaves exact. In the bank's trace the first task with the 32-cycle cadence is `tp1` (the transposer's second
commutator): from it on, every internal stream carries each frame as 16 consecutive words, 32 cycles apart,
and the input side (`x`, `fft_in_reg`, `fft_tp0`) stretches to 28-30 cycles a frame, full. Not traced
further: which part of `tp1`, and whether it is `2L/R` at every `L` (measured at `L = 64` only).

**Proposed fix for A (for the user: `SsrFft`'s model):** in `waveflow/dsp/ssr_fft/hw.py`'s task-level pysim,
the task that sets the cadence starts a frame that does not follow its previous one back to back no earlier
than the previous frame's start plus `2L/R`. Before it: the gap sweep at `L = 16, 256, 1024` and a trace of
`tp1` alone, to place the rule on the right task and confirm `2L/R`. Then `tests/dsp/ssr_fft/test_hw.py`
gains a gapped-frames pin beside its back-to-back one (which should not move), and `timing.md` its row.
Expected effect: most of the gap (A is 92-103% of it) -- to be re-measured, with Step 3 again.

**The rest, where each belongs** (Step 4, after A, since A sets the loop's cadence and so the path the rest
lie on): B, C, D are the example's own stages -- their `run_iter`s charging what their HLS bodies spend (`pre`
copying as the words arrive, as the arbiter now does; `post`'s loop entries; `front`'s block read); G is
part of D (the first block) and the drivers' start; E is open question 7 (a design calibrates at its own
width and frame: the table has no width in its basis, so the bank's 128-bit arbiter needs it); F and H are
`StreamDemux`'s, which stores and forwards and decides at once in pysim, where its HLS body copies through
after a few cycles' decision.

**Tooling gaps met** (worked around in `fidelity.py`, not fixed in the framework):
- `CompositeTopSpec.trace_manifest()` declines a top with two tasks of one body and template arguments (here
  `dmx_st` and `dmx_crd`), and `load_trace`'s fallback to `<body>_U0` would bind the wrong instance: Vitis
  2025.1 named the bank's three `stream_demux_hdr_task`s three ways (`..._184_2_0_8_..._U0`, `..._32_1_0_8_
  ..._U0`, and the bare `stream_demux_hdr_task_U0`), so `dmx_ans`'s nets would bind to `dmx_crd`'s pins.
  `fidelity.py` binds each channel's instance-prefixed nets by the channel's own name (unique), as the one
  net ending `_U<k>_<channel>_<role>`.
- `waveflow.utils.vcd.split_framed_word` unpacks only whole-64-bit payloads, so a framed 184-bit channel
  (`ans`) has no burst view; its beats are split by count (`Resp` + 12 words a frame).

## 6. Cause A: the rule, confirmed first (2026-11-22, lab machine, Vitis / xsim 2025.1)

The user approved the fix with three conditions: confirm the rule at `L = 16, 256, 1024`; check it is
deterministic with random per-frame gaps (if the intervals spread, report before modelling); trace which task
sets it. `python -m examples.os_filterbank.fidelity --fft-gaps 64 16 256 1024`: `SsrFft` alone, ping-pong,
runtime direction, its own system flow (`ssr_fft.rtl`), every frame of every run bit-exact; results in
`work/osfb_fidelity/fft_rule_L<L>_runtime.json`.

**Constant gaps** (12 frames; the RTL's interval against `max(2L/R, L/R + g)`, `L/R` at `g = 0`):

| L | gaps measured | rule holds | pysim today |
|---|---|---|---|
| 16 | 0, 1, 2, 3, 4, 6, 8 | at 0, 4, 6, 8; **not at 1-3**: intervals alternate 4 and 8 (8, 8, 8, 4 at 3) | `L/R + g` |
| 64 | 0, 1, 2, 4, 8, 15, 16, 24, 32 | every gap | `L/R + g` |
| 256 | 0, 1, 2, 4, 32, 63, 64, 96, 128 | every gap | `L/R + g` |
| 1024 | 0, 1, 2, 4, 128, 255, 256, 384, 512 | every gap | `L/R + g` |
| 4096 | 0, 1, 2, 4, 512, 1023, 1024, 1536, 2048 (measured after the approval) | every gap | `L/R + g` |

**Random gaps** (24 frames; a third of the gaps 0, the rest uniform in 1 .. `2L/R`), frame by frame:

| L | rule, interval by interval | rule, total of 23 | pysim today (`L/R + g`), total |
|---|---|---|---|
| 16 | exact on 16 of 23; off by -4, -2, -1 | +17 over the RTL's 156 | **156: exact**; per interval within 3 |
| 64 | exact on 23 of 23 | exact (636) | -47 (up to 14 an interval) |
| 256 | exact on 23 of 23 | exact (2,198) | -262 (up to 59) |
| 1024 | exact on 23 of 23 | exact (9,587) | -1,288 (up to 222) |
| 4096 | exact on 23 of 23 | exact | -- (the rule since modelled) |

**Which task** (`fft_tasks`: a traced run, frames 1 cycle apart): at `L = 64, 256, 1024` it is the **last
transposer commutator**, `tp{S-2}` (`tp1`, `tp2`, `tp3`; block size `L/16`): its input stream is full and
each frame on it spans `2L/R - L/16` cycles (28, 112, 448), while its output and every stream after it carry
each frame as `L/R` consecutive words, exactly `2L/R` apart. The streams before it stretch less (`tp0`:
28 / 124 / 508; `in_reg` 31 / 127 / 511), all full.

**`L = 16` is different** (there the last transposer commutator is also the first, `tp0`, block size 1): the
FFT never stalls its input (every frame accepted in 4 cycles) and on the random run loses no throughput -- its intervals sum to
`L/R + g` exactly (at a constant gap of 1-3 cycles it falls up to 13 cycles behind over 11 frames: corrected after the approval, row "4 (A, the model)") -- but each frame's latency is 40 to 43 cycles, depending on the gaps before *and after* it
(a frame after a 1-cycle gap takes 43 when the next comes 3-6 cycles later, 40 when the next follows back to
back): an arrival-phase effect, like `VitisFft`'s vendor core (`docs/guide/vitis_l1/fft/timing.md`). So no
single exact rule covers every length.

**For the user, before modelling** (the approval's condition): the measured picture suggests applying the rule
on the last transposer commutator where its block size `L/16` is 4 or more (`L >= 64`, exact on every
frame measured), and leaving `L = 16` as today (exact in total; within 3 cycles a frame, stated as its
error), rather than a mean with an error for every length. `L = 4096` is not measured (a long csynth).

## 7. Open questions for the user

1. **`SsrFft`'s half-rate interval after a gap is a real throughput loss in its HLS**, not only a pysim
   error (`tp1` at `L = 64`, Section 5). A shared FFT rarely gets its frames back to back -- in os_filterbank
   `pre` always leaves a cycle -- so at RTL it runs at half its rate there. Worth fixing in the HLS
   separately from this plan (the pysim would then model the fixed hardware). The user's call.
2. **The os_filterbank page's claim that `post` is why the FFT is under half busy** (`docs/examples/
   os_filterbank/index.md`, "the FFT is busy under half the time, and `post` is why") should be re-checked
   against cause A. A first look (Step 3): on the RTL's critical path the binding hop is the FFT's own
   32-cycle rhythm (71 of 149 hops on `long`), and `post` averages about 31 cycles a frame (about 43 on a
   forward pass, about 20 on an inverse), under the FFT's 32. Not yet re-measured as the page would need.

## 8. Where Step 4 stopped (2026-11-22): two decisions for the user

**1. How `SsrFft`'s gap rule counts a gap.** The rule is measured exactly (Section 6); what pysim cannot see is
when the source would offer its next frame at RTL, because pysim's links hold a whole frame and do not stall a
source word by word (H4). Two countings, each exact for one kind of source:

| counting | the drivers (the pins) | the bank, `test` / `long` (after B, C, D) |
|---|---|---|
| from the previous frame's start (as built): `prev + max(2L/R, L/R + g)` | exact, 23 of 23 at every `L` | 1,277 / 2,839 (+19.7% / +17.1%) |
| from the arrivals: `max(arrival, prev + 2L/R)` | 17-19 of 23 (6-8% short in total) | **1,039 / 2,425 (-2.6% / +0.0%)** |

A driver that waits its gap after the FFT takes its last word needs the first; a source whose frames come when
their data does (the bank's `pre`) needs the second. Exact for both needs pysim to stall a source as the RTL
does -- a change to how links admit a burst (`waveflow/hw/interface.py`), which this plan asks about first.
Options: (a) the arrival counting, the pins restated against driver stalls pysim does not model (their 6-8%
named as that); (b) keep the start counting, the bank's +17-20% named as the same limit; (c) a link stall in
`interface.py` (a framework change, large); (d) a per-use choice on `SsrFft` (a build field: which source it
has). The recommendation: (a), since a design's FFT is fed by its kernels, not a testbench driver, and the bank
lands on its RTL.

**2. E, F, H need framework changes**, none made:
- **E** (+5 / +1): the arbiter's decision table is at 32 bits and 1-burst frames; the bank's is 128 bits and
  2-burst. `plans/stream_dispatch.md` open question 7 decided "a design calibrates at its own width and
  frame", but the table's basis has no width, so that needs the calibration's basis extended
  (`waveflow/streams/timing.py`, the stream-block fixture).
- **F and H** (+4 / +3 and -12): `StreamDemux` in pysim decides at once and stores and forwards; its body
  decides in a few cycles and copies through. The fix is the demux's own model -- commit on a first word and
  copy as words arrive, as Stage 8 did for the arbiter and the dispatcher -- and a decision table calibrated as
  Stage 7's. A library block's model, as the plan allows, but a change to its pysim semantics and its
  calibration machinery, so asked first.

G is D's (the first block's read, fixed with it). After these, Step 5 (the gate and its tolerance).
