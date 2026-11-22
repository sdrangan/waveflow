# Plan: StreamDispatch -- one job stream split across a pool of identical workers

**Status: ACCEPTED 2026-11-22. Stages 0-3 DONE in pysim and codegen. Stage 4 DONE at Vitis / Vivado
2025.1: the four pool gates pass, 48 resource records filed, the cost per job measured at RTL. Stage 6
DONE: every probed top meets 250 MHz on xczu48dr. Stage 7 DONE: the decision cycles in pysim, opt-in on the
platform, exact for each block alone. Open questions 5-7 DECIDED by the user 2026-11-22. **Stage 8 DONE
2026-11-22 (word against burst): both blocks commit on a frame's first word in pysim; the timed pool at one
credit now assigns first-free's jobs as the RTL does (all to worker 0), and runs 6-15% long (was 7-17%);
the decision tables did not move; every gate passes.** Stage 5, the curated example, not started: it waits
for the user (the hand-off's Step 6).**

## Why

Waveflow teaches one direction of sharing: several clients into **one** kernel, `StreamArbiter -> K ->
StreamDemux` (`docs/guide/systems/shared_kernels.md`, `examples/os_filterbank`). It has nothing for the
opposite direction: **one** job stream spread over a **pool** of identical workers, each job to a worker
that is free. The guide's "replicate K" means a fixed copy per client; nothing chooses a worker at run time.

A pool sits between the two extremes a design already has -- one shared unit, or a unit per client -- and
is the answer when jobs have data-dependent lengths (a static round-robin split then leaves one worker busy
and another idle). It came up designing the evaluation problem for `plans/dse_arch.md`: a multi-channel
spectrum sensor whose confirm passes vary in number per block, where "M FFTs serving C channels" is an
architecture option between "one shared FFT" and "one per channel". Without a block for it, an agent hits
exactly the gap the Stage 0 rule of `plans/shared_kernel.md` says to extend rather than work around.

## The block

```text
                      +--m_out_0--> worker 0 --+
s_in --> [ StreamDispatch ]--m_out_1--> worker 1 --+--> [ StreamArbiter ] --m_out--> results
          [   credits     ]   ...                  |    [               ] --m_sel--+
          [   per worker  ]--m_out_n-1-> worker n-1+                               |
             ^                                                                     |
             +----------------------------- s_free <-------------------------------+
```

- One framed input `s_in`; `n_out` framed outputs `m_out_0 .. m_out_<n-1>`, one per worker; one credit
  input `s_free`. A **frame** is `frame_bursts` TLAST bursts, as the arbiter's and the demux's.
- **Credits**: each worker starts with `credits` (C) of them, the jobs it may hold at once. A job sent to
  worker k takes one of k's; a word `k` on `s_free` gives one back.
- **One action a firing, a returned credit first**: if `s_free` holds a word, take the credit back; else, if
  a job waits and some worker holds a credit, send the job whole to the worker the policy picks; else
  nothing. Credits first for the reason feedback goes first in a shared kernel: they are what unblocks.
- **The policy**, fixed at build time: `"first_free"`, the lowest-numbered worker holding a credit;
  `"round_robin"`, the search starts one past the last worker chosen.
- `s_free`'s word is a worker's index -- exactly what a `StreamArbiter` merging the workers' results
  writes on its `m_sel`. So `dispatch -> workers -> arbiter`, `m_sel -> s_free`, closes the loop with no
  glue. Results come out tagged by whatever the job carried (a `tx_id`), in completion order.

## Decisions

- **D1. One credit stream carrying the worker's index**, not a credit stream per worker: one port, one
  `empty()` test, and it composes with the arbiter's `m_sel` directly. A design whose workers do not merge
  through an arbiter merges their done words through one (`frame_bursts=1`) and takes its `m_sel`.
- **D2. Two policies**, `first_free` and `round_robin`. A `least_loaded` policy (the fewest outstanding)
  differs from `first_free` only for C > 1; left for when a design needs it.
- **D3. C is one build parameter for every worker**, 1 to 255. Identical workers are the block's premise.
- **D4. No `m_sel` output.** The block owns its outputs, so nothing after it needs the choice to route; a
  result's owner is in the job's own header. One fewer side stream that must be drained (the arbiter's
  second rule).
- **D5. A word on `s_free` naming no worker, or a worker with no job outstanding, is ignored and counted**
  (`bad_credits`), as the demux counts a frame it drops: the block cannot be pushed past C outstanding.
- **D6. The RTL gate is the pool** (`DispatchPool`: dispatch -> n links -> arbiter, `m_sel -> s_free`),
  checked order-insensitively. Which worker takes a job depends on when credits come back, as which input
  an arbiter takes depends on arrivals; so no recording is pysim's word for word, and `compare` cannot be
  the gate. The gate runs through `system_xsi`, then asserts the RTL output's frames are the golden's (the
  inputs' frames) as a multiset, none lost, none doubled, each whole -- and the same of pysim's.

## Rules a design keeps

1. **The credit link holds every outstanding credit**: `s_free`'s link at least `n_out * credits` words
   deep. Otherwise the arbiter blocks writing `m_sel`, stops reading the workers, a worker's input fills,
   the dispatcher blocks mid-job and stops reading `s_free`: a cycle of waits.
2. **Announce a job only when it is all there**: the block commits on a job's first word (the arbiter's
   first rule, the same reason).
3. **A credit means room for a whole job**: a worker with C credits must accept C jobs without anything
   downstream of the dispatcher moving first -- its input link and its own buffering hold C frames, or it
   drains its input independently of its output.

## Where it lives

| what | where |
|---|---|
| the block, its model | `waveflow/streams/dispatch.py` (`StreamDispatch`, `StreamDispatchStep`) |
| the HLS body | `waveflow/streams/src/stream_dispatch_task.h` |
| the gate's composite | `waveflow/streams/rtl.py` (`DispatchPool`, `make("pool", ...)`) |
| tests | `tests/streams/test_dispatch.py` (pysim, codegen; `-m xsi` gates) |
| guide | `docs/guide/streams/dispatch.md`; rows in `streams/index.md`, `systems/library.md`; a worker-pool section in `systems/shared_kernels.md` |
| skill | a line in `waveflow/skills/waveflow-shared-kernel/SKILL.md` (and its two copies) |

## Stages

| stage | what | done when |
|---|---|---|
| 0 | this plan | accepted |
| 1 | the block: model, body, step, the model's tests (policies exact on a backlog, frames whole, credits never exceeded, a bad credit counted, refusals) | pysim tests pass |
| 2 | `DispatchPool` and its codegen; pysim on data-dependent workers (a test worker whose time depends on the job) showing first-free keeps every worker busy where a static split does not | pysim tests pass; the generated top holds both tasks and the loop |
| 3 | the guide page and the rows; the shared-kernel section and skill line | the page builds into the `kb` index |
| 4 | RTL gates (`-m xsi`): the pool, both policies, n = 2 and n = 4 (the arbiter's hang needed four), C = 1 and C = 2; csynth resources and the dispatch cost per job | gates pass at Vitis / Vivado 2025.1; numbers filed on the guide page |
| 5 | a curated example: jobs of data-dependent length on M workers with their own HLS body, results tagged by `tx_id`, against the same work on one worker | pysim, RTL, an exact cycle count |
| 6 | the RFSoC probe (250 MHz on `xczu48dr`), as the arbiter's | timing and resources recorded |
| 7 | the decision cycles in pysim (open question 1, decided; *Stage 7* below) | pysim's idle cycles between jobs equal the RTL's at every calibrated point -- DONE; the pool's assignment as the RTL's -- not met (open question 6) |
| 8 | word against burst: both blocks commit on a begun burst (open question 6, decided; *Stage 8* below) | the pool's first-free assignment as the RTL's; the tables recalibrated; rule 2's stall shown in pysim -- DONE |

## Progress log

| date | stage | what happened |
|---|---|---|
| 2026-11-22 | 0 | Plan written with the user, from the `dse_arch` evaluation design (a pool of FFTs as an architecture option). |
| 2026-11-22 | 1 | `StreamDispatch` (model, `stream_dispatch_task.h`, `StreamDispatchStep`). pysim: first-free sends 0,0,1,1,2,2 to three workers with two credits and nothing returned, round-robin 0,1,2,0,1,2; a 3-burst job never split; a surplus credit and one naming no worker counted (`bad_credits` 2) and no worker past its credits. The body's logic under g++ with stand-in `hls_stream.h` / `ap_int.h` / `streamutils_hls.h` (`tests/streams/cpp_mock/`, logic only, not csim) decides as the model's rules on a scripted run, both policies, C = 1 and 2. The held counts are one packed register, so their reset is a scalar's. |
| 2026-11-22 | 2 | `DispatchPool` (`rtl.make("pool", ...)`): every job once and whole at n = 2..4, C = 1..3, 1- and 2-burst jobs, both policies; it states no `golden` (the in-order step would fail on a timing-decided order) but `expected_frames`, a multiset. The pool's top has both tasks, the loop, and the credit link at `n * C` (12 at 4 x 3). The link is named `credit`: `free` would shadow the C library's. Through the system DAG to `pysim` (4 workers, C 3, 2-burst jobs, the long workload): the trace's multiset is the inputs'. **Data-dependent workers** (a test worker 20 cycles a word; 18 jobs, every third 16 words, the rest 2; three workers): first-free, C 1, last result at cycle 1,012, jobs 8/5/5; a fixed split (round-robin, C 16), 2,065, jobs 6/6/6 -- 2.0x. |
| 2026-11-22 | 3 | `docs/guide/streams/dispatch.md`; rows in `streams/index.md` and `systems/library.md`; *A pool of workers* in `systems/shared_kernels.md` and a pointer in its "Replicate K first"; the shared-kernel skill (a table row, the checklist's replicate item, its description) and the `stream_shared` frame's deciding question. `waveflow kb search "split jobs across several workers pool"` finds the page first. |
| 2026-11-22 | 4 | The four `-m xsi` gates written (`tests/streams/test_dispatch.py`: pool2 first-free; pool3 round-robin C 2 with 2-burst jobs; pool4 first-free and pool4 round-robin C 3 on the long workload), run through `system_xsi` and checked as a multiset, RTL and pysim. `WANT_XSI_GATES` 281 -> 285 (282 -> 286 once `main`'s Stage 7 gate was merged in). **Not run: no Vitis / Vivado here.** For the lab: `pytest tests/streams/test_dispatch.py -m xsi`; then csynth resources at 2 and 4 workers and the dispatch cost per job (the arbiter's `arbiter_timing.py` is the model) onto the guide page. |
| 2026-11-22 | lab 1 | On the lab machine (Ubuntu 24.04, Vitis / Vivado 2025.1 at `/tools/Xilinx/2025.1`, system g++ 13.3): Step 1, the baseline. `pytest tests/streams -m "not vitis and not xsi"`: 82 passed, 12 deselected. |
| 2026-11-22 | lab 2 | Step 2, the RTL gates (Vitis HLS / Vivado 2025.1, `DEFAULT_PART` at 100 MHz). On the first run every pool's RTL `m_out` was the inputs' jobs as a multiset -- csynth clean, no hang, none lost, doubled or split: pool2 first-free, 14 jobs, last word at cycle 138; pool3 round-robin C 2, 14 2-burst jobs, 250; pool4 first-free on the long workload, 40 jobs, 526; pool4 round-robin C 3, 40 2-burst jobs, 1,044. All four then failed on the next line, reading `pysim_traces/`: `through="system_xsi"` runs a step and its ancestors only, and `pysim` is not one, so pysim never ran. The fix is the test's driving, not its assertions: it also runs `through="pysim"`. Then **4 passed** from a cold build (96 s); pysim's runs end at cycles 89, 166, 376 and 696 (the RTL's decision cycles, which pysim does not model: Step 4). Regression: `test_arbiter_demux.py -m xsi`, 5 passed. No change to D1-D6, the body or the model. |
| 2026-11-22 | lab 3 | Step 3, resources. `rtl.make("dispatch", ...)`, the dispatcher alone as its own `ap_ctrl_none` top (`sdsp{ff,rr}<n>_w<W>_f<NB>_c<C>`); its own scenario now puts an empty bundle on `s_free`, which the harness's driver needs. `python -m waveflow.streams.calib dispatch`: 48 points (2 and 4 workers; 32, 64, 128 bits; 1- and 2-burst jobs; 1 and 4 credits; both policies), xczu48dr at 4 ns, Vitis HLS 2025.1: 48/48 clean in 810 s, every loop II = 1, no DSP or BRAM. Published into `xczu48dr_250mhz_vitis2025_1` (144 files, 48 keys, `platform.json` unchanged); the lookup answers `EXACT` on 48 distinct keys. LUT at one credit, one burst, 32 / 64 / 128 bits: 449 / 513 / 641 (2, first-free), 642 / 706 / 834 (2, round-robin), 882 / 1,010 / 1,266 (4, first-free), 1,365 / 1,493 / 1,749 (4, round-robin): about a LUT a bit a worker (a copy pipeline an output). Routed (`rfsoc_probe` `dsp16_w128_rr`, Vivado 2025.1, out of context, 4 ns): 16 workers x 128 bits, round-robin, 6,611 LUT, 14,098 FF, WNS +0.224 ns. Tables on `dispatch.md`, *Its resources*. |
| 2026-11-22 | lab 4 | Step 4, the cost per job (`waveflow/streams/dispatch_timing.py`, modelled on `arbiter_timing.py`; the default part at 100 MHz, Vitis HLS / xsim 2025.1; 40 jobs of 8 words). **The dispatcher alone**, credits ample: idle cycles between jobs 3 / 3 / 5 / 5 at 2 / 4 / 8 / 16 workers under first-free, 4 / 5 / 10 / 20 under round-robin; every gap in a run the same, none inside a job; pysim 0 everywhere. Round-robin at 16 workers: the search over five states and the count update over eleven (open question 3). **The pool at one credit**: idle cycles between jobs 5 / 5 first-free and 7 / 9 round-robin at 2 / 4 workers (a credit firing between); from a credit's write to the next job's first word on its worker, 9-11 cycles first-free (4-6 waiting for the copy in progress, 5 to react), 27-28 / 64 round-robin (the worker's turn); pysim at least 1. At RTL first-free put all 40 jobs on worker 0 (the arbiter returns a credit as it starts a copy), where pysim spread them 20 / 20 and 13 / 12 / 9 / 6. The RTL round trip reads the pool's links off a traced run: `load_trace` could not bind it, because Vitis 2025.1 spelled the tasks' word types `framed_word` where the manifest predicts `framed_word_32`; it now binds the one instance with the task's `<body>_<template args>` prefix (two tests in `tests/utils/test_trace.py`). On `dispatch.md`, *What it costs a job*. The decision cost is not added to pysim (open question 1). |
| 2026-11-22 | lab 5 / 6 | Step 5, the RFSoC probe (`rfsoc_probe.py`: `Probe.credits`; the dispatcher alone at 2 and 4 workers under both policies, 32 bits, 2-burst jobs, 4 credits; `dsp16_w128_rr`; the pool as its gates build it at 2 first-free C 1 and 4 round-robin C 3). xczu48dr-ffvg1517-2-e at 4 ns, out of context; csynth Vitis HLS 2025.1 (budget 2.92 ns), place and route Vivado 2025.1. All seven II = 1 and timing met. csynth estimate / routed clock / WNS: dispatcher 2 first-free 2.699 / 2.416 / +1.584 ns; 2 round-robin 2.891 / 2.664 / +1.336; 4 first-free 2.889 / 2.708 / +1.292; 4 round-robin 2.883 / 3.306 / +0.694; 16 x 128 round-robin 2.870 / 3.776 / +0.224; pool 2 first-free 2.763 / 1.812 / +2.188; pool 4 round-robin C 3 2.877 / 2.539 / +1.461. Routed LUT / FF 363 / 579, 334 / 589, 651 / 1,035, 671 / 1,128, 6,611 / 14,098, 284 / 317, 674 / 891. Slowest states: the round-robin search (`next_k + j`, its wrap, the free test) at 2; a count's add -> select -> compare elsewhere; worst routed paths 76-96% routing. The pool at 4 has its dispatcher named plainly (`stream_dispatch_task_U0`) already. Not done: XSI at 250 MHz, so the idle cycles between jobs are 100 MHz figures. On `dispatch.md`, *At 250 MHz on the RFSoC*. |
| 2026-11-22 | 7 | **The decision cycles in pysim** (the user's two choices: opt-in by platform, the arbiter too). `waveflow/streams/timing.py`: one lookup `TimingModel` a block on the platform (`components/stream_arbiter_task.decision`, basis `n, rr`; `components/stream_dispatch_task.decision`, basis `n, rr, kind` -- a job, or a credit taken back), attached at `pre_sim` when the block names `platform_dir`; `run_iter` waits the firing's cycles after choosing, before acting (the arbiter before `m_sel`). `platform_dir` is a build field (the harness's copy keeps it) and context to the module key (the filed resource records still match); `DispatchPool` and `ArbiterDemuxPair` pass it on. Untimed by default: every existing count and gate unchanged. Calibrated by `waveflow/calib/fixtures/stream_blocks.py` (registered with the other fixtures): each block alone compiled for xczu48dr at 4 ns, under XSI, 2, 4, 8 and 16 ports under both policies (Vitis HLS / xsim 2025.1), then pysim on the platform's model, fit, again: converged in two passes, every point exact. Idle cycles at 4 ns -- arbiter, priority / round-robin: 3 / 3, 3 / 4, 7 / 8, 8 / 8; dispatcher job gap, first-free / round-robin: 3 / 3, 3 / 5, 6 / 10, 5 / 15; a credit firing: 4 / 4, 4 / 5, 6 / 7, 10 / 12 (each run's gaps uniform outside the two credit blocks, and the two blocks agreeing to the cycle). Spot checks at 4 ports: 128 bits with 2-burst frames, every gap a cycle longer, credits unchanged; credits naming no worker, the same cost as valid ones. Tests: `tests/streams/test_stream_timing.py`, 8 fast (pysim reproduces every committed row; refusals of an unmeasured point and of another clock; the key unchanged; the pool keeps the platform) and 2 `-m xsi` gates (dispatcher and arbiter at 4, round-robin, re-measured against the table): pass; `WANT_XSI_GATES` 286 -> 288. **The pool at one credit, 250 MHz** (`dispatch_timing --pool-fidelity`, traced): 40 jobs end at cycle 524 at RTL at 2 workers (682 at 4 round-robin); untimed pysim 376 (28-45% short), timed 611 (730): 7-17% long. The excess is the credit firing: in the pool, `s_free` an internal FIFO, a credit adds 2 cycles to a job gap at 2 workers first-free and 4 at 4 round-robin, against the table's 4 and 5 (open question 5). First-free at RTL sends every job to worker 0 (the arbiter commits on a job's first word and returns its credit during the copy); pysim, timed or not, spreads them 20 / 20 -- the model's grain, burst against word (open question 6). *What it buys*, timed, 4 workers, 24 jobs at 250 MHz: 974 against the fixed split's 2,073 (2.1x; untimed 2.3x). `pytest -m "not vitis and not xsi and not gem5 and not sionna"`: 6,081 passed, the same nine environment failures as Step 7; the 14 stream XSI gates, named by file: 14 passed. |
| 2026-11-22 | lab 7 | Step 7. `pytest -m "not vitis and not xsi and not gem5 and not sionna"`: 6,073 passed, 142 skipped, 9 failed, 423 quarantined. The nine fail the same way on the base commit `cf286f0f` exported fresh, so they are the environment's, not this work's: a fresh tree lacks the untracked example XSI workspaces, `gen/` trees and markov's `sync_status.json`, and the MCP init tests resolve the venv's interpreter to `/usr/bin/python3.12`, which has no `mcp` (`test_xsi_workspace_copies::test_there_are_workspaces_to_check`, two in `test_markov_figures`, four in `test_init_project`, one each in `test_retrieval_eval` and `test_usage_index`). The run also rewrote two `rfsoc4x2_bfm_250mhz` `vitis_fft` `corpus.csv` files in their last float digits: restored, not committed. `pytest tests/streams -m xsi`: 12 passed (arbiter-demux 5, broadcast 2, dispatch 4, framer 1), none skipped; that session exits 1 only on the whole-suite floor (12 < `WANT_XSI_GATES` 286: `conftest._is_narrowed` does not count a directory), and the same gates named by file pass with exit 0. No `-m xsi` test added or removed, so `WANT_XSI_GATES` stays 286. `library.md`'s row now has the measured timing and resources. Step 6, the curated example, not started: it is the user's call. |
| 2026-11-22 | 8 | **Word against burst** (lab machine, Vitis / Vivado / xsim 2025.1; *Stage 8* below has the record). `interface.wait_any(..., begun=True)` -- the one addition to `interface.py` -- counts a begun burst from its first word's arrival; `arbiter.copy_burst` takes it then (`begin_read(pending_ok=True)`), starts the output at once and ends it no earlier than the input's last word; `StreamArbiter` and `StreamDispatch` use both, always. **The pool at one credit, 250 MHz** (`dispatch_timing --pool-fidelity`; RTL unchanged, every run `same_order`): first-free's jobs per worker, RTL / timed pysim, 40 / 0 and 40 / 0 at 2 workers (Stage 7: 20 / 20), 40 / 0 / 0 / 0 and 40 / 0 / 0 / 0 at 4 (Stage 7: 22 / 18 / 0 / 0); round-robin 20 / 20 and 10 x 4 both sides. End cycle, RTL / timed pysim: 524 / 601 at 2 first-free (+14.7%, Stage 7 611), 524 / 602 at 4 first-free (+14.9%, 612), 524 / 601 at 2 round-robin (+14.7%, 611), 682 / 721 at 4 round-robin (+5.7%, 730): 6-15% long, was 7-17%; the rest is the in-pool credit firing (open question 5, decided). Untimed pysim 362 (was 376), first-free 24 / 16 and 18 / 11 / 7 / 4 (the arbiter's `m_sel` cycle a frame, with no dispatcher cost, puts it behind). **The decision tables did not move**: `python -m waveflow.calib.fixtures.stream_blocks` from the committed RTL spans converged in one pass, every point exact, files byte-identical (each block alone, backlogged, is decision-limited). **Rule 2's stall in pysim**: three tests (the dispatcher, both policies: a job begun at 10 and held open to 200 keeps a credit returned at 20 unread until 200; the arbiter: a frame held open wins at its first word and holds a higher-priority input) -- all fail on the previous model. **Speed**: the 4-worker pool, 40 jobs, untimed, `run_sim` alone, median of 25: 271 -> 312 SimPy events and 2.53 -> 3.28 ms at 8 words a job, 237 -> 313 and 4.32 -> 5.18 ms at 512 (same machine, same script) -- still per burst, not per word, about a quarter dearer (a first-word wake a begun burst). Moved, asserted nowhere: What it buys 1,012 / 2,065 -> 969 / 2,035 (3 workers), timed 974 / 2,073 -> 928 / 2,043 (4); the pool gates' pysim end cycles 89 / 166 / 376 / 696 -> 80 / 150 / 362 / 682; os_filterbank's pysim 969 -> 885 (test), 1,934 -> 1,766 (long). **Gates**: the stream XSI gates named by file 14 passed (none skipped); os_filterbank `-m xsi` 6 passed (recordings equal pysim's, `LAST_STATUS` unchanged); ring_stub `-m xsi` 8 passed. No gate's recorded number moved; no gate added, `WANT_XSI_GATES` unchanged (288). `pytest -m "not vitis and not xsi and not gem5 and not sionna"`: 6,088 passed, 143 skipped, 10 failed -- Step 7's nine environment failures, and `tests/linalg/test_no_example_imports.py::test_waveflow_gained_only_the_manifest`, which diffs against the local `main` (`3619245`) and sees Stage 7's 52 decision-table files as unlisted gains; this stage adds no file under `waveflow/`, so the base fails it too. The run rewrote the two `rfsoc4x2_bfm_250mhz` `vitis_fft` `corpus.csv` files again: restored. |

## Lab hand-off: Stages 4-6 on a machine with Vitis / Vivado 2025.1

**For the agent on the lab machine.** Stages 0-3 are done (commit `701e3da`, this branch,
`ccr-62d70dbe-679k63`); this section is your task. Read first, whole: this plan (D1-D6, the three
rules, the progress log), `docs/guide/streams/dispatch.md`, `waveflow/streams/dispatch.py`,
`waveflow/streams/src/stream_dispatch_task.h`, `DispatchPool` in `waveflow/streams/rtl.py`, and
`tests/streams/test_dispatch.py`. Work on this branch; commit and push after every step, so progress is
visible without the session.

**Toolchain:** Vitis HLS / Vivado **2025.1**; name the version next to every number recorded. Build in a
short directory on Windows (csynth fails silently past the path limit). Any job that must outlive your
session goes under `setsid` / `nohup`, never as a session background task (G1's measuring processes died
with their session, `plans/dse_cg.md` 2026-11-09).

**State:** model, body, step, `DispatchPool`, 28 fast tests passing; the four `-m xsi` gates written and
**never run**; `WANT_XSI_GATES` already 286 (after merging `main`), so a full `-m xsi` run fails until they run.

### Step 1: the baseline

```bash
pip install -e ".[dev]"
pytest tests/streams -m "not vitis and not xsi"      # expect all pass (82)
```

### Step 2: the RTL gates (Stage 4)

```bash
pytest tests/streams/test_dispatch.py -m xsi -v
pytest tests/streams/test_arbiter_demux.py -m xsi -v   # regression: rtl.py's BODY_STEPS changed
```

Each pool gate runs through `system_xsi`, then asserts that the RTL's `m_out` and pysim's each hold
exactly the inputs' jobs, as a multiset: whole, none lost, none doubled (D6; not `compare`, because which
worker takes a job is timing's).

**If a gate fails, classify it first; never weaken, skip or change what a gate asserts.**
- **csynth fails.** Suspects: `#pragma HLS reset` on the packed `ap_uint<8*N>` register `held`, the
  variadic output pack, the `range()` writes in the unrolled loops. Fix the body, and keep the g++ check
  (`test_the_body_decides_as_the_model`) passing.
- **A hang** (short counts, the run stops). First the copy loop: it is the arbiter's and must keep its
  exact shape and `INLINE` (`stream_arbiter_task.h` says why). Then the credit link's depth (`n * credits`).
  If n = 4 hangs and n = 2 passes, compare with the arbiter's four-input hang
  (`plans/shared_kernel_rfsoc_timing.md`).
- **A wrong multiset** (a job lost, doubled or split): a body bug. Reproduce it in the g++ check, if it
  can be, before fixing.

Any fix keeps the model and the body agreeing: credits first, one action a firing, the same search order.
If a gate cannot pass without changing D1-D6, push the diagnosis and stop: that decision is the user's.

### Step 3: resources (Stage 4)

- Add a kind `"dispatch"` to `rtl.make()`: the dispatcher alone as its own `ap_ctrl_none` top, as
  `"arbiter"` is (no gate; its csynth is the block's resources, its RTL run the block's timing).
- Add `dispatch` to `waveflow/streams/calib.py`, as `demux` is, and file records in the packaged platform
  `xczu48dr_250mhz_vitis2025_1` (csynth at 4 ns) for n_out 2 and 4; bitwidth 32, 64, 128; frame_bursts 1
  and 2; credits 1 and 4; both policies.
- One routed run at n_out 16, 128 bits: LUT, FF, WNS.
- Tables on `dispatch.md` under *Its resources*, in `demux.md`'s format.

### Step 4: the cost per job (Stage 4)

- `waveflow/streams/dispatch_timing.py`, modelled on `arbiter_timing.py`: the dispatcher alone, a backlog
  of jobs, credits ample so none is ever waited on, every output read as it comes. Measure the idle
  cycles between one job's last word and the next's first, across the outputs, at RTL and in pysim, for
  n_out 2, 4, 8, 16 under both policies.
- The credit round trip: the pool at C = 1, the idle cycles a worker between jobs.
- Record both on the guide page under *What it costs a job*. **Do not** add the decision latency to the
  pysim model (the arbiter's is not modelled either): report the gap, and list "model it or not" in
  *Open questions* below for the user.

### Step 5: the RFSoC probe (Stage 6)

Extend `waveflow/streams/rfsoc_probe.py` with the dispatcher: 250 MHz on `xczu48dr-ffvg1517-2-e`, out
of context; csynth's estimate, the routed critical path and WNS at 2 and 4 workers.

### Step 6: the curated example (Stage 5) -- ask the user before starting

Report Steps 2-5 first. If the user agrees, `examples/worker_pool/`, following `examples/os_filterbank`:
- jobs of data-dependent length (for example an iterative kernel whose iteration count its data sets);
- a worker kernel with its own HLS body (`docs/guide/vectorization/hls/loop_optimization.md`);
- M workers behind `StreamDispatch`, merged by `StreamArbiter`, `m_sel` back as the credits;
- results carrying the job's `tx_id` in a `Resp` header, in completion order, and a `Status` stream
  (`docs/guide/systems/command_response.md`); a golden checked per `tx_id`;
- pysim and RTL, an exact cycle-count gate on a fixed scenario;
- one worker against M, and first-free against a fixed split;
- `docs/examples/worker_pool/index.md` with an `example_dir:` key.

### Step 7: record and push

- A progress-log row a step (numbers with the tool version); update the status line at the top.
- `docs/guide/systems/library.md`'s `StreamDispatch` row (timing and resources now "not measured").
- Any `-m xsi` test added or removed: `WANT_XSI_GATES` in `tests/conftest.py`, with a dated note.
- `pytest -m "not vitis and not xsi and not gem5 and not sionna"` and `pytest tests/streams -m xsi`;
  commit, push, and report what passed, what failed (with its output) and what was skipped.

**Throughout:** never hand-edit a generated file; never pack words by hand; when Waveflow lacks
something, propose the extension rather than work around it; stop and ask before any change to D1-D6.

## Stage 7, the decision cycles in pysim

Decided by the user 2026-11-22 (open question 1); the shape below was proposed, and the user chose (a)
opt-in by naming the platform and (b) the arbiter in the same stage.  Built as proposed, with two changes
the measurements forced: the credit firing is measured from the dispatcher alone (credits in two blocks on
`s_free`), not from the traced pool, whose every gap holds one credit, so a job firing cannot be told from
a credit firing there; and the model attaches at `pre_sim`, so a codegen copy of a block, rebuilt at the
class's default clock, is not refused for a clock it never runs at.  What was built and found is the
progress row "7"; what is left is open questions 5-7.

- **Where the delay goes.** In `StreamDispatch.run_iter`, `yield self.timeout(self.timed_delay(features))`
  once the firing has chosen and before it acts: before taking a credit back, and before a job's copy.
  The features are the firing's kind (credit or job), `n_out` and the policy. With no model attached
  `timed_delay` returns 0, which is today's behaviour.
- **Calibrated where the numbers belong.** The 100 MHz gaps above were measured on the gates' part. The
  arbiter's analysis found the RFSoC's likely a cycle larger, and a measured number belongs to its tool
  version and clock. So: XSI of the dispatcher alone compiled for xczu48dr at 4 ns, the gap per (`n_out`,
  policy), and the credit firing's cost from the traced pool at one credit. Filed in the packaged platform
  `xczu48dr_250mhz_vitis2025_1` as a lookup per measured point, as `vitis_l1`'s FFT timing is per measured
  `L`, with an `UNCALIBRATED` refusal for a point not measured. The attaching field must stay out of the
  module key, so the 48 resource records still match.
- **The arbiter too**, by the same mechanism (its open item in `plans/shared_kernel_rfsoc_timing.md`). In
  a pool, which worker takes a job depends on when the arbiter writes `m_sel`; timing only the dispatcher
  would leave the pool half-timed.
- **Done when** pysim's gap equals the RTL's at every calibrated point (a fast test against the records,
  one `-m xsi` check); the pool at one credit then sends first-free's jobs where the RTL does (all to worker
  0 at 2 and 4 workers); *What it buys* on the guide page re-measured with the model on.
- **For the user before starting:** (a) opt-in by naming the platform, as `vitis_l1`'s FFT does (the
  recommendation: every existing pysim count and gate stays as it is), or on by default; (b) the arbiter in
  the same stage.

## Open questions

1. **DECIDED 2026-11-22 (the user): model the decision cycles in pysim** -- BUILT, Stage 7.
   At RTL, 100 MHz, Vitis / xsim 2025.1: 3 / 3 / 5 / 5 idle cycles between jobs at 2 / 4 / 8 / 16 workers
   under first-free, 4 / 5 / 10 / 20 under round-robin, 2 to 4 more when a credit firing comes between;
   pysim spends 0. The condition was that pysim not slow down, and it does not: pysim's cost is per burst,
   not per word (a link admits a burst in one event whatever its length, `interface._admit_blocking`;
   `wait_any` waits on arrivals, it does not spin). Measured: the 4-worker pool, 40 jobs, 3.7 ms of wall
   time at 8 words a job and 3.8 ms at 512, for 57 times the simulated cycles. A model adds one `timeout` a
   firing.
2. A `least_loaded` policy (D2): only if a design needs it.
3. **Round-robin's cost at 8 and 16 workers** (Step 4): 10 and 20 idle cycles a job, against first-free's 5.
   **DECIDED 2026-11-22 (the user): accepted for now, optimized later.** The proposal, not built (the body
   and the model would change together, and the g++ check and the gates re-run): search a per-worker "has a
   credit" mask, each bit a constant part-select, rotated by `next_k` and priority-encoded; update the one
   count by `win` with a single indexed write. The search order, and so D2's policy, is unchanged.
4. **Fixed in passing, outside the block** (Step 4): `waveflow.utils.trace.load_trace` binds a task whose
   word types Vitis 2025.1 spelled other than predicted (`framed_word` for `framed_word_32`, in the pool and
   the arbiter-demux pair) by its `<body>_<template args>` prefix, the one instance with it. The prediction
   in `composite_gen._vitis_type_name` is unchanged; it is right for markov's chain core.
5. **DECIDED 2026-11-22 (the user): no general fix now.** A design that needs the decision cost exact inside
   a composite calibrates it in context, by tracing that composite. **The decision cost inside a composite** (Stage 7). The table is each block alone, every port a
   top-level AXI-Stream port; inside the pool the dispatcher's credit firing costs 2 to 4 cycles less, so
   the timed pool runs 7-17% long. Options: calibrate in context -- the pool, traced, gives a job gap with
   one credit between (`G + K`), not `G` and `K` apart, so the credit firing's in-pool cost would be taken
   as the pool's gap less the alone `G`; or a composite bench putting each block's ports on internal FIFOs;
   or a context feature in the basis (which ports are at the top). For the user: which, if any.
6. **DECIDED 2026-11-22 (the user): build the fix -- BUILT, Stage 8 below.** **Word against burst** (Stage 7). The RTL blocks commit to a frame on its first word; pysim moves a
   burst whole, so in a pool the arbiter's credit comes back later in pysim than at RTL, and first-free
   picks other workers (RTL: every job to worker 0 at one credit). No delay fixes it. pysim has the
   mechanism (`begin_read(pending_ok=True)`, a writer's begun bursts, `plans/pipelined_ops.md`): both
   blocks would commit on a begun burst and copy as its words arrive -- a change to their pysim semantics,
   which would also let pysim show rule 2's stall (a job announced before it is all there). For the user.
7. **DECIDED 2026-11-22 (the user): no extension now.** A design calibrates at its own width and frame. **Width and frame** (Stage 7). At 128 bits with 2-burst frames every gap is a cycle longer than the
   32-bit, 1-burst table's; not yet told apart (width or bursts), and not in the basis.

## Stage 8: word against burst -- DONE 2026-11-22 (the task as written, then its record)

**For the agent picking this up:** a fresh session. Read this plan whole first (D1-D6, the rules, Stage 7
and its progress-log row, the open questions), then `waveflow/streams/timing.py`, `waveflow/streams/arbiter.py`,
`waveflow/streams/dispatch.py`, `waveflow/hw/interface.py`'s `wait_any` and `begin_read`, and
`plans/pipelined_ops.md`. Work on `ccr-62d70dbe-679k63` (pull first); commit and push after each step.

**The problem** (open question 6). The RTL blocks commit to a frame on its first word and copy as the words
arrive, so in a pool the arbiter returns a worker's credit during the copy. pysim moves a burst whole: a block
sees a burst only once delivered (`wait_any`: "A burst whose transfer has only begun does not count"), so the
credit comes back later by about one burst. At one credit a worker, first-free at RTL sends every job to worker
0; pysim, timed or not, spreads them 20 / 20. No delay fixes it.

**Why it matters:** `plans/dse_matrix_sched.md` (Sections 5-7) puts a pool of CG units behind `StreamDispatch`
with a deadline. With one credit a worker, a late credit adds about one result burst (hundreds of words) to
every job's idle gap -- a few percent of a ~3,400-cycle job, where the deadline decides. Until this lands that
study uses at least 2 credits a worker.

**What to build:**
1. `StreamArbiter` and `StreamDispatch` commit on a **begun** burst and copy its words as they arrive -- the
   mechanism already in pysim (`begin_read(pending_ok=True)`, a writer's begun bursts, `plans/pipelined_ops.md`).
   These two blocks need a way to wait on a begun burst. Keep TLAST counting, frames whole, D1-D6 unchanged.
2. **Always on**, timed or untimed: it is fidelity, not an option.
3. **Recalibrate the decision tables** (`python -m waveflow.calib.fixtures.stream_blocks`): their residual is
   rtl - pysim, so they move with pysim's semantics. Same platform, 4 ns, Vitis / xsim 2025.1; converge as before.

**Stop and ask the user:**
- if any existing gate's recorded number moves (os_filterbank's timing gates, ring_stub, the pair, the pool,
  anything with an exact cycle count): report before and after for each, before re-recording anything;
- before any change to `waveflow/hw/interface.py` beyond adding a way to wait on a begun burst: describe it.

**Done when:**
- the pool at one credit, 250 MHz (`dispatch_timing --pool-fidelity`): first-free's assignment matches the RTL's;
  report each worker's count (RTL against pysim) and the pool's end cycle (RTL against timed pysim, against
  Stage 7's 7-17% long);
- a test shows rule 2's stall in pysim: a job announced before it is all there stalls the block, as at RTL;
- pysim's speed holds: the pool's wall time at 8 and 512 words a job (Stage 7: 3.7 / 3.8 ms);
- every existing test and the stream XSI gates pass; `WANT_XSI_GATES` with a dated note if gates are added;
- a progress-log row, the status line, `docs/guide/streams/dispatch.md` and `arbiter.md` (the word-against-burst
  caveat removed or restated), `plans/dse_matrix_sched.md` precondition 2 (the 2-credit rule lifted if met);
  committed and pushed. Then stop and report.

### Stage 8 record (2026-11-22, lab machine, Vitis / Vivado / xsim 2025.1)

**DONE.** Numbers in the progress-log row "8".

- **The wait** (`waveflow/hw/interface.py`, the only change there): `wait_any(*eps, begun=False)`. With
  `begun=True` an endpoint is ready when it holds a delivered burst, or a begun one (`pending_burst`) whose
  first word has arrived (`pending_burst[1] <= now`, to a millionth of a cycle); the wait wakes on a burst
  posted or delivered (the arrival slots, as before) and on a timeout to the earliest first word still on its
  way. The default path is unchanged (ring stub's `Mux` and every other caller). `FreeRunMod.wait_any`
  passes `begun` through.
- **The copy** (`waveflow/streams/arbiter.py`, `copy_burst`): `begin_read(pending_ok=True)` (the burst at its
  first word, its words in hand), `m_out.begin(words)` at once, `end_read` (the input's delivery), then
  `m_out.end` -- whose delivery is `max(start + n cycles, now)`, so the output's last word is never before
  the input's. Both blocks copy every burst of a frame with it; TLAST counting, frames whole and D1-D6
  unchanged. Always on.
- **Why the tables did not move.** Each block alone, on a backlog, fires on delivered bursts after its
  first few frames (its output, with the decision, is slower than its input), and a delivered burst copies
  in exactly the old time; `SKIP_JOBS` / the first `n` frames cover the start. So the residuals are the same,
  and the refit from the committed RTL spans gives byte-identical files.
- **Why untimed pysim still spreads first-free** (24 / 16): there the arbiter spends a cycle a frame
  writing `m_sel` and the dispatcher none, so the arbiter drifts behind and a credit is late for the next
  decision. That is the untimed model's timing, not its grain; timed, both blocks spend their measured
  cycles and the assignment is the RTL's.
- **Cost:** about a quarter more wall time and 15-30% more events on the pool (a first-word wake and a
  delivery wait a begun burst), still independent of the burst's length.
- **Left:** the in-pool credit firing (open question 5, decided: calibrate in context) is now the whole of
  the timed pool's excess -- about 2 cycles a job at 2 workers first-free (the table's credit firing 4,
  in the pool 2): 40 jobs, 77 cycles over the RTL's 524.
- **os_filterbank's end cycle moved away from the RTL's** (asked by the planning session; measured, no
  code changed). The run's end is its last word on any output. RTL, xczu48dr at 250 MHz, Vitis / xsim
  2025.1: 1,067 (`test`), 2,424 (`long`). Untimed pysim, Stage 7 -> Stage 8: 969 (-9.2%) -> 885 (-17.1%),
  and 1,934 (-20.2%) -> 1,766 (-27.1%). `OsFilterBank` has no `platform_dir`, so it cannot name the
  platform. With the platform set on its arbiter from a script (the only block with a table; measured at
  32 bits with 1-burst frames, against this arbiter's 128 bits with 2-burst frames): 973 (-8.8%) -> 901
  (-15.6%), and 1,944 (-19.8%) -> 1,800 (-25.7%). Short before and after, never over.
  **Why:** the arbiter's change; the bank has no dispatcher. Each frame's 16-word payload (26 frames in
  `test`, 72 in `long`: every block crosses twice, forward from its front and inverse from the feedback
  link) used to reach `pre` 13-17 cycles after its last word reached the arbiter. That is store and
  forward: the payload was copied only once it was delivered. Now it arrives 0-3 cycles after (cut-through,
  as the HLS body does). Frames queued at the start shifted by exactly 14 cycles each. The end moved by 84
  and 168 cycles, 6 and 12 times that. pysim was already short, and the old extra burst per pass was a cost
  the RTL arbiter does not have; it offset part of costs pysim does not model elsewhere in this loop. The
  arbiter's decision cycles are a small part of that gap: timing them adds 16 and 34 cycles, which leaves
  166 and 624 short. Those were not attributed here. `docs/examples/os_filterbank/index.md` quotes only
  RTL cycles, so it is unchanged.

