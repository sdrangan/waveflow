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
| 2026-11-22 | 4 | The four `-m xsi` gates written (`tests/streams/test_dispatch.py`: pool2 first-free; pool3 round-robin C 2 with 2-burst jobs; pool4 first-free and pool4 round-robin C 3 on the long workload), run through `system_xsi` and checked as a multiset, RTL and pysim. `WANT_XSI_GATES` 281 -> 285. **Not run: no Vitis / Vivado here.** For the lab: `pytest tests/streams/test_dispatch.py -m xsi`; then csynth resources at 2 and 4 workers and the dispatch cost per job (the arbiter's `arbiter_timing.py` is the model) onto the guide page. |
