# Plan: StreamDispatch -- one job stream split across a pool of identical workers

**Status: ACCEPTED 2026-11-22. Stages 0-3 DONE in pysim and codegen (this session, no vendor tools);
Stage 4's RTL gates written and pending on a machine with Vitis / Vivado 2025.1; Stages 5-6 open.**

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

## Open questions

1. **Whether pysim models the dispatcher's decision cycles between jobs** (measured in Step 4, for the user
   to decide). At RTL, 100 MHz, Vitis / xsim 2025.1: 3 / 3 / 5 / 5 idle cycles between jobs at 2 / 4 / 8 / 16
   workers under first-free, 4 / 5 / 10 / 20 under round-robin, 2 to 4 more when a credit firing comes
   between; pysim spends 0. The mechanism would be the arbiter's proposal (`FreeRunMod.add_timing_model`,
   `timed_delay` in `run_iter` before the copy, calibrated per `n_out` and policy at the platform's clock).
   Not done: the plan said report, not model. A consequence to weigh: the cost changes which worker takes a
   job -- in the pool at one credit, first-free put every job on worker 0 at RTL and spread them in pysim.
2. A `least_loaded` policy (D2): only if a design needs it.
3. **Round-robin's cost at 8 and 16 workers** (Step 4): 10 and 20 idle cycles a job, against first-free's 5.
   Proposed, not built (the body and the model would change together, and the g++ check and the gates re-run):
   search a per-worker "has a credit" mask, each bit a constant part-select, rotated by `next_k` and
   priority-encoded; update the one count by `win` with a single indexed write. The search order, and so
   D2's policy, is unchanged.
4. **Fixed in passing, outside the block** (Step 4): `waveflow.utils.trace.load_trace` binds a task whose
   word types Vitis 2025.1 spelled other than predicted (`framed_word` for `framed_word_32`, in the pool and
   the arbiter-demux pair) by its `<body>_<template args>` prefix, the one instance with it. The prediction
   in `composite_gen._vitis_type_name` is unchanged; it is right for markov's chain core.
