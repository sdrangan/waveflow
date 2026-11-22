# Plan: why os_filterbank's pysim runs 17-27% short of its RTL, and a gate so it cannot drift again

**Status: ACCEPTED 2026-11-22 (the user), for a lab machine with Vitis / Vivado 2025.1. Steps 1-3 DONE
2026-11-22 (`examples/os_filterbank/fidelity.py`; the record is Section 5). Step 4 STOPPED for the user: the
cause of 92-103% of the gap is `SsrFft`'s model (its interval after a gap), which this plan says to ask about
before changing.**
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
