# Plan: why os_filterbank's pysim runs 17-27% short of its RTL, and a gate so it cannot drift again

**Status: ACCEPTED 2026-11-22 (the user), for a lab machine with Vitis / Vivado 2025.1. Not started.**
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
