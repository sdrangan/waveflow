# Plan: long-term beamforming on a shared-memory accelerator, with signal-level inputs and a design-space exploration

**Status:** In progress (approved by the user 2026-10-28; *Start* 2026-10-28) <!-- Ready only after the user approves this written plan -->
**Complexity:** L3 Complex. Three engines, an ingest DMA and a CPU on one bus, a branch merge, signal-level channels, fixed-point and architecture axes, calibrated models validated against Vitis; everything is undoable with git. Scores: scope 3 · clarity 3 · novelty 3 · dependencies 3 · verification 3 · risk 2
**Created:** 2026-10-28 · **Updated:** 2026-10-28 (revised after the independent plan review)
**Plan file:** `plans/ltbf_dse/ltbf_dse.md` · **Lessons learned:** `plans/ltbf_dse/ltbf_dse_lessons.md` (created in step 0.1)
**Spec (written in Phase 1, frozen at the 👁 gate):** `plans/ltbf_dse/ltbf_dse_spec.md`
**Branch:** `paper/ltbf_dse`, created in step 0.1 from `main` (`b0de5a26`)
**Deadline:** none. Compute is bounded by the budgets in §2.

> **For the agent executing this plan:** read *Rules for the doer* first. The
> plan is self-contained. Paths are repo-relative, commands are exact, and
> secrets are named by environment variable only (this plan needs none).

## Rules for the doer

1. Before changing anything, re-run the readiness commands in §8. Stop if anything required is ❌
   blocked. Run every Waveflow, pytest, Sionna, Vitis or gem5 command inside the **activated** venv,
   `source .venv/bin/activate` (it sets `XILINX_VIVADO` and `LD_LIBRARY_PATH`; calling
   `.venv/bin/python` directly makes every XSI gate error).
2. Do one step at a time (§10), except the ⏱ background jobs, which run while the next steps
   proceed. A step is done only when its exit condition is verified with the stated method; show the
   command and its result as evidence. Before changing code, read it and its callers. Check the
   authoritative docs for any version-sensitive API (versions in §6). Before building a tool, script
   or integration the plan didn't foresee, search for an existing one (start with
   `examples/ltbf_dse/` (copied from `paper/cg_dse`), `git show paper/cg_dse:examples/ltbf_cg/hw/<file>`,
   `examples/mimo_cg/`, `examples/markov/`, `examples/cpu_sched/`, `examples/vecunit_cg/`,
   `waveflow/linalg/`, `waveflow/vecunit/`, `waveflow/cpu/`, `waveflow/sw/`, `waveflow/hw/mm_device.py`)
   and propose it first.
3. After each step, tick it in §10, add a line to the §15 progress log, and append anything
   surprising to `plans/ltbf_dse/ltbf_dse_lessons.md`.
4. When something fails, find the root cause from the error, logs, tests and actual state. No random
   fixes. Deviation policy: investigate up to **2** tested hypotheses, then stop and report. **Never
   change a test's random seed, tolerance or confidence level to make it pass.**
5. **Pause only when required** (user, 2026-10-28). Keep working through milestones; stop only for
   (a) a merge conflict other than the two pre-approved in step 0.2, (b) a 👁 gate, (c) a compute job
   that would exceed its budget in §2, (d) a change under `waveflow/` that §14 does not pre-approve,
   (e) anything in §12, or (f) a spike result that makes a step in §10 impossible as written. Batch
   the questions; while waiting, continue with work that doesn't depend on the answer. Reviewer agents
   run in the background and never block; their findings go to the user at the next stop.
6. **Long jobs run detached** (`plans/mimo_cg/mimo_cg_lessons.md`: a harness background task is killed
   after 30 min by default and 2 h at most). Every job longer than 30 min is launched as
   `setsid nohup <cmd> > examples/ltbf_dse/logs/<job>.log 2>&1 &`, is resumable per item (it skips
   finished items on restart), and has its PID and start time recorded in §15. Poll it with the
   Monitor tool (an until-loop on the log or a done-file), with long `ScheduleWakeup` fallbacks if
   that tool is available; never with a foreground sleep.
7. **CPU, memory and disk discipline.** At most 8 busy workers in total. Every Python worker sets
   `OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1`. Large csynth runs run at most 2 at
   a time with a watchdog that kills a run above 10 GB of RSS. Run rendering and sweeps with
   `nice -n 10`. Keep ≥ 20 GB of disk free; signal caches ≤ 30 GB (§2).
8. Never perform an action listed in §12 without its backup in place and fresh approval from the user
   at that moment, unless §14 records a pre-approval.
9. **Never merge, push, deploy or publish unless the user asks.** The one planned merge (step 0.2) is
   pre-approved for its two known conflicts only. VS Code has published a branch on its own before:
   run `git status -sb` at each milestone and report it if the branch shows a remote.
10. **No data in git.** The Denver scene copy, drops, rendered signals, grids, sweep outputs, CSVs,
    figures, logs and build trees stay local under `examples/ltbf_dse/`'s gitignored directories.
    Commit only code, tests, `examples/ltbf_dse/.gitignore`, pre-registration files that a framework
    API requires to be committed (each listed in §15; the first is `examples/ltbf_dse/cpu/sweep_plan.csv`),
    the plan, the spec and the lessons file. Pre-registered sets are fixed by committing their SHA-256
    in §15.
11. **No data from outside the repo.** The scene, drops and A53 calibration are copied once from
    `examples/ltbf_cg/data/` (inside this checkout, local) in step 0.4, and only the copies are used.
    The paper repo `/home/wirelesslab914/ali/salsa-ltbf` is read-only, read for its text and code
    (the algorithm's source of truth), never imported and never a data source.
12. **Commits** use the repo's effective identity (`ali-rasteh`); never pass `--author` or set
    `GIT_AUTHOR_*`/`GIT_COMMITTER_*`; end every message with
    `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage by explicit path, never
    `git add -A` or `git add .`. Never stage the root `.gitignore` (except through the step 0.2 merge)
    or `docs/repo_docs/`.
13. Name the tool version next to every measured number (Vitis/Vivado 2024.1, sionna-rt 2.2.0,
    gem5 v25.1). A criterion whose target is missed is ⚠️ in §16, never ✅, even if the number is
    reported.
14. Never hand-pack a word: headers, job descriptors and data go through their `DataList` schema or
    `waveflow.linalg.lanes`; never edit a generated file (`include/`, `gen/`, `xsi/`, `*_proj/`).
15. Finish with §16: check every acceptance criterion, run the regression check, and write the
    completion report.

## 1. Goal

Build, in Waveflow, the receiver of *Scalable Long-Term Beamforming for Massive Multi-User MIMO*
(`/home/wirelesslab914/ali/salsa-ltbf`) on a **shared-memory accelerator**: a systolic array
(`waveflow/linalg`), a vector unit (`waveflow/vecunit`) and a CPU (`waveflow/cpu`) on one bus,
the CPU assigning and scheduling the work, each engine reading its operands from and writing its
results to a shared memory. Feed it **real signals**: UEs' NR-like slots rendered through the
ray-traced Denver channels by the Sionna producer (`waveflow.rfenv`, merged from `feat/sionna-prod`),
brought into the shared memory by an ingest DMA. Then run a **complete design-space exploration** of
that architecture.

**The claim the DSE supports** (user, 2026-10-28): the cheapest design of this architecture (on
the RFSoC 4x2's xczu48dr at 250 MHz plus its A53) whose post-equalization SINR stays within a loss
budget of exact float LTBF and whose long-term update finishes within T_LT, together with the largest
real-time load (bandwidth × antennas × UEs) that design sustains, ingest included; from calibrated
models validated against Vitis and system RTL, with concrete design findings. Writing the manuscript
is out of scope.

### 1.1 The algorithm (paper §III–IV; code `salsa/salsa/salsa_cap.py`)

Per sector, `N_rx` antennas, UEs with `N_s = 1` stream, projection rank `r ∈ {1, 2}`:

- **Long term, once per T_LT = 10 ms.** SRS snapshots `Ĥⱼ ∈ ℂ^{N_rx × N̄}` (`N̄ = N_s·N_SRS = 8`) of
  every connected UE; `Ĥα = [Ĥ₁, …]/√N_SRS` (`αⱼ` is already in the power-controlled channel:
  **never scale twice**); `Q̂ = I + ĤαĤαᴴ`. Multi-right-hand-side CG on `Q̂Aᵢ = Ĥᵢ` (regime 1) or
  `Q̂X = I`, symmetrized, then `Aᵢ = XĤᵢ` (regime 2), `t` iterations from `X₀ = 0`: `S = Q̂D`,
  `ωⱼ = ‖rⱼ‖²/(dⱼᴴsⱼ)`, `X += D·diag(ω)`, `R −= S·diag(ω)`, `νⱼ = ‖rⱼ'‖²/‖rⱼ‖²`,
  `D = R + D·diag(ν)` (the paper code's stop rule: a column stops when `dᴴs = 0` or `‖r‖² < 1e-10`).
  `S` is **explicit** (`S = Q̂D`, `Q̂` formed) or **factored** (`S = D + Ĥα(ĤαᴴD)`). Then
  `Mᵢ = ĤᵢᴴAᵢ` (8×8, symmetrized), its top-r eigenvectors `Φᵢ`, and `Gᵢ = (AᵢΦᵢ)ᴴ` (r × N_rx).
- **The projected interference-plus-noise covariance** (default; the spec may choose a DMRS-based
  estimate instead, §14): `R̃ᵢ = Gᵢ(Q̂ − Q̂ᵢ)Gᵢᴴ = GᵢGᵢᴴ + Σ_{j≠i}(GᵢĤα,ⱼ)(GᵢĤα,ⱼ)ᴴ` (r × r), where
  `Ĥα,ⱼ` is UE j's block of `Ĥα`; it needs `GᵢGᵢᴴ` and `GᵢĤα` only (`GᵢĤᵢ` is `√N_SRS` times UE i's
  columns of `GᵢĤα`).
- **Real time, every slot.** Projection `zᵢ[n,k] = Gᵢ y[n,k]` for every resource element; the
  reduced-domain effective channel `h̃ᵢ[n,k]` estimated from the DMRS REs of `zᵢ`; the per-RE MMSE
  `x̂ᵢ = h̃ᵢᴴ(h̃ᵢh̃ᵢᴴ + R̃ᵢ)⁻¹zᵢ` (a scalar divide at r = 1, a 2×2 solve at r = 2).
- **What the paper reports** (`compute_sinr_lt`, `salsa_cap.py:231–302`): per UE, the frequency mean
  of the linear per-RE SINR `γ = s/(1 − s)`, `s = hpᴴ(GQGᴴ)⁻¹hp`, with the **true per-frequency**
  `Q = I + H_cH_cᴴ` (a genie). The float chain computes this genie SINR too, so the paper check
  (step 2.4) is like for like; the DSE's metric is the realizable receiver's measured SINR (§2).

### 1.2 The architecture (user, 2026-10-28; the diagram from the meeting of 2026-10-02, plus the ingest DMA)

```
                IRQ (data)
  ┌───────────┐ ───────► ┌──────────────────────────┐   ┌────────────────────┐   ┌──────────────────────────┐
  │ Resp FIFO │          │        Processor         │   │     Ingest DMA     │   │        Shared Mem        │
  │ (the CPU's│          │  A53 model, 1-4 cores:   │   │ front-end port:    │   │  on-chip BRAM/URAM       │
  │  queue)   │          │  scheduler, Jacobi eig,  │   │ grid tiles from the│   │  (a DDR model prices the │
  └─────▲─────┘          │  r x r math, Python      │   │ O-RU into a ring   │   │   sets that won't fit)   │
        │                │  channel-estimation calls│   │ of tile buffers    │   └────────────▲─────────────┘
        │                └────────────┬─────────────┘   └─────────┬──────────┘                │ slave
        │                             │ master                    │ master                    │
  ══════╪═════════════════════════════╪════════════ AXI-MM crossbar ══════════════════════════╪═══════════
        │      slave │      slave │       ▲ master      slave │      slave │      ▲ master
        │       ┌────▼───┐   ┌────▼───┐   │            ┌──────▼─┐   ┌──────▼─┐    │
        │       │Job FIFO│   │  Ctrl  │   │            │Job FIFO│   │  Ctrl  │    │
        │       │        │   │ (regs) │   │ mem port   │        │   │ (regs) │    │ mem port
        │       └────┬───┘   └────┬───┘   │            └────┬───┘   └────┬───┘    │
        │       ┌────▼────────────▼───────┴───┐        ┌────▼────────────▼────────┴──┐
        │       │       Systolic Array        │        │          Vec Unit           │
        │       │  SystolicEngineMem (R x C)  │        │ memory front end + VecUnit  │
        │       └──────────────┬──────────────┘        └──────────────┬──────────────┘
        │                      │ done                                 │ done
        └──────────────────────┴───────────────────┬──────────────────┘
                                                    └── tile-ready (from the Ingest DMA)
```

| Element | Bus role | What it is |
|---|---|---|
| Processor | master | the software host: a `waveflow.sw.SwHost` (it holds the `MMIFMaster` and the IRQ sinks, and has a C++ twin for XSI) whose threads charge their compute to a `waveflow.cpu.Processor` on the shipped A53 platform; it sleeps on IRQs and never polls. `Processor` alone is not a bus master (`examples/cpu_sched` reaches the bus through separate dispatchers); spike 1.2 e fixes the pattern |
| Shared Mem | slave | `MemoryMod` on the crossbar (BRAM/URAM window), one address space for every buffer; size and banking are knobs |
| Ingest DMA | master | the front-end port (user, 2026-10-28): writes each grid tile (a few symbols × a subcarrier chunk × all antennas) the Python front end produces into a ring of tile buffers in Shared Mem, with real bus and memory-port cost, and pushes a tile-ready record into the Resp FIFO; a full ring is a counted real-time overrun |
| Job FIFO ×2 | slave | one per engine, a `QueueIn` view (`waveflow/hw/mm_device.py`) the CPU writes job descriptors into; its room IRQ paces the CPU |
| Ctrl ×2 | slave | one per engine, a `RegBank` view the CPU uses to configure the engine and read its status. **Systolic Ctrl is thin** (user, 2026-10-28): enable, status and error code, job and cycle counters, job defaults the wrapper applies; its number formats are fixed per build (`SystolicEngineMem`'s `a`, `b`, `c` are build parameters), so one format triple serves every systolic job and the triple is a DSE knob. **Vector-unit Ctrl:** the program table (where each program lives in Shared Mem), the result shift where the build has one, status and error code, counters |
| mem port ×2 | master | each engine's `m_axi` read and write paths into Shared Mem (`SystolicEngineMem`'s feeder and writer; the vector unit's new front end) |
| Resp FIFO | slave | the CPU's response queue: both engines push one done record per job, and the Ingest DMA one record per tile; each record names its source, `tx_id` and status. A `QueueOut` view the CPU reads; its data IRQ wakes the CPU. The push path (direct done streams into a merge in front of the queue, or routed over the crossbar) is fixed in the spec; direct streams are the default (§14) |

**How a pipeline runs.** The CPU writes a job into an engine's Job FIFO; the engine reads its
operands from Shared Mem, computes, writes its result to Shared Mem, and pushes a done record into
the Resp FIFO; the CPU, woken by the IRQ, releases the jobs that depended on it, and the other
engine reads that result from Shared Mem. One CG iteration (factored form), for example:

```
CPU                         Systolic                       Vec Unit                  Shared Mem
 |--job: T = HαᴴD ---------->|                                                           |
 |                           |---- read Hα (as stored for MUL_AH), D ------------------->|
 |                           |---- write T ----------------------------------------------->|
 |<-- done (Resp FIFO IRQ) --|                                                           |
 |--job: U = Hα T ---------->|---- read Hα (as stored for MUL), T; write U -------------->|
 |<-- done ------------------|                                                           |
 |--job: CG step (S = D + U, ω, ν, X, R, D updates) --------->|-- read X,R,D,U ---------->|
 |                                                            |-- write X,R,D ----------->|
 |<-- done ----------------------------------------------------|                           |
 (t times; real-time tile jobs, at higher priority, interleave between these jobs)
```

### 1.3 Where each step runs (user, 2026-10-28)

| Phase | Python (zero simulated time) | Ingest DMA | Systolic | Vector unit | CPU (A53 model) |
|---|---|---|---|---|---|
| Inputs | `rfenv` renders the antenna samples; the front end (stands for the O-RU, O-RAN 7.2x) removes the CP and FFTs each antenna into grid tiles | writes each tile into Shared Mem (timed), tile-ready → Resp FIFO | | | |
| Long term | **SRS channel estimation** → `Ĥⱼ`, `Ĥα` stored in the layouts the systolic jobs need (§9 c), with op counts | the SRS symbol's tiles | explicit `K = ĤαĤαᴴ`; per iteration `KD` or `ĤαᴴD`, `Ĥα(·)`; `Mᵢ = ĤᵢᴴAᵢ`; `Gᵢᴴ = AᵢΦᵢ`; `G_stack G_stackᴴ`, `G_stack Ĥα` | the CG step (the `+D` the engine can't add, norms, `ω`/`ν` divides, updates) | scheduler; 8×8 Jacobi eigendecomposition (the `paper/cg_dse` kernel); `R̃ᵢ` and other r × r math |
| Real time | **DMRS channel estimation** on `zᵢ` → `h̃ᵢ[n,k]`, with op counts | the slot's tiles | `Z = G_stack·Y` per tile (`G_stack`: all UEs' `Gᵢ`, `N_UE·r × N_rx`, padded to the tile) | per-RE MMSE weights and `x̂ = Wz` | scheduler |
| Measurement | demapping, EVM, BER, post-equalization SINR, the paper's genie SINR | | | | |

**Channel estimation stays in Python** (user, 2026-10-28) but is written for later complexity
calculations: every estimator is explicit matrix operations through an op-counting helper
(`examples/ltbf_dse/opcount.py`) that logs each operation's kind, shapes, complex MACs, divides and
bytes read and written. In the system simulation the host calls it as a **zero-time** stage when its
inputs are in Shared Mem, and it writes its outputs there; its counts are logged, not costed. Its
operations are matrix products of known shapes, so they can later be priced on the systolic engine's
models. In the RTL gate the C++ host replays its outputs from a file written by the pysim run (§9 e).

### 1.4 The scheduler on the CPU (supporting infrastructure, not a contribution of the paper)

- **Job graphs.** Per sector and T_LT, and per slot, the host builds a graph: nodes are systolic,
  vector-unit and CPU jobs and the Python estimation stages; edges are data dependencies through
  Shared Mem buffers; a real-time graph's roots are the Ingest DMA's tile-ready records.
- **Dispatch.** A list scheduler: a job is ready when all its predecessors are done; ready jobs are
  written into an engine's Job FIFO while it has room (room IRQ), so an engine never idles on the CPU.
  On a Resp FIFO IRQ the host pops every record, marks the jobs finished and releases their
  successors.
- **Priorities.** Two classes: real-time tile work (deadline: the tile's slot, 125 µs) ahead of
  long-term work (deadline T_LT); earliest deadline first inside a class; long-term jobs fill the
  gaps. No preemption inside an engine job, so the long-term job tile size bounds how long real-time
  work waits (a knob).
- **Shared Mem allocation.** Buffer offsets planned per graph from buffer lifetimes; the ingest ring
  of tile buffers is sized as a knob (not whole slots: one 64-antenna sector slot is about 2.8 MB);
  checked against the memory size before dispatch (the DDR model takes over when the working set
  doesn't fit).
- **Ctrl writes** only when a job needs a configuration different from the engine's current one;
  counted.
- **Flow control and errors.** Jobs in flight plus ring tiles ≤ the Resp FIFO depth; an engine error
  status is fatal (`docs/guide/systems/command_response.md`): stop dispatching, report.
- **Cost.** Every scheduler operation is a `SwFunction` on the `Processor`. Calibrated families
  already exist on the A53 platform: ready-list operations (`sched_ops`), `dispatch`
  (`waveflow/cpu/calib/kernels/dispatch.c`, rows in `sweep.py`) and context switches; only
  **release** (successor bookkeeping) is a new kernel, calibrated by a pre-registered gem5 campaign.
- **Policies built:** the dynamic two-class list scheduler, and static in-order as the baseline. For
  the RTL gate the policy runs deterministically (§9 e).

## 2. Requirements and constraints

| Type | Item |
|---|---|
| Must | The architecture of §1.2: one Shared Mem; per engine a Job FIFO written by the CPU and a Ctrl register space; one Resp FIFO at the CPU fed by both engines and the Ingest DMA; the Ingest DMA as the only way the grid enters Shared Mem; every inter-engine hand-off through Shared Mem (user). |
| Must | The engines are `waveflow.linalg.SystolicEngineMem`, `waveflow.vecunit.VecUnit` and `waveflow.cpu.Processor` on the shipped A53 platform, **unchanged**; the Job FIFOs, Ctrl, the Resp path, the vector unit's memory front end and the Ingest DMA are **example-level** in `examples/ltbf_dse/hw/` (user). |
| Must | `feat/sionna-prod` merged into `paper/ltbf_dse` (step 0.2), and `waveflow.rfenv` used to render the received signals the hardware consumes (user). |
| Must | Inputs: the frequency-domain grid from a Python front end (CP removal, FFT); **channel estimation (SRS and DMRS) in Python** with op counts (§1.3) (user). Projection and equalization in hardware (user). |
| Must | Scenario (from `paper/cg_dse`): the 100 locally traced Denver drops, two snapshots T_LT apart (the drops' keys `a0…`/`a1…`: snapshot 0 for the long-term statistics, snapshot 1 for the slot), 3 sectors × 10 UEs, `N_s` = 1, `N_SRS` = 8, 3.5 GHz, 120 kHz SCS, 100 MHz (792 subcarriers, FFT 1024, 122.88 Msps, CP 72 samples ≈ 0.59 µs), 8×8 / 16×16 / 32×32 as sub-arrays of one trace, SINR targets [−6, 3] and [−6, 14] dB, r ∈ {1, 2}. Doppler inside a snapshot is zero (§14). |
| Must | Signals: snapshot 0 carries one SRS symbol (all 30 UEs); snapshot 1 one PUSCH slot (DMRS + uncoded QPSK). Each UE is timing-advanced to its serving sector's earliest path; everything else is kept as traced, so delay spread beyond the CP (about 2.9% of UE–sector links) is real inter-symbol interference, reported (user, 2026-10-28). Thermal noise through `rfenv`. No LDPC. |
| Must | Rendering **on the fly** per drop, array and target inside each job (power control depends on both, and storing every grid would take about 77 GB); a cache of at most 30 GB for reuse; deterministic (§4 AC3). |
| Must | Accuracy: per-UE post-equalization SINR measured from the data REs (`1/mean(MSE) − 1`) of the hardware's arithmetic (finite t, fixed point) against **exact float LTBF on the same signals and estimates** (direct solve, float64): its loss in dB, median and 10th percentile; budgets 0.1, 0.25, 0.5 dB; uncoded BER and EVM as secondary metrics. |
| Must | Timing: the long-term update of all three sectors within T_LT = 10 ms (also scored at 5 and 1 ms); the real-time path's sustained RE rate per (N_rx, N_UE), **ingest included**, against the 88.7 M RE/s of 100 MHz (14 symbols × 792 subcarriers per 125 µs); the fraction of the band sustained is reported where the full band is not. |
| Must | Cost: DSP, LUT, FF, BRAM, URAM utilization fractions of the xczu48dr and their maximum; CPU utilization and energy (A53 platform's models) reported. |
| Must | DSE axes. Algorithm: t ∈ 1…10; word width from 10 bits up to the widest whose systolic accumulator `waveflow.linalg.matmul.acc_format` accepts at K = N_rx (about 26 bits with guard bits at K = 1024; spike 1.2 b fixes the cap); `Q̂` explicit or factored; regime 1 or 2; r. Architecture: systolic tile (`Mmax`, `Kmax`, `Nmax`, `Pmax`, `R × C`), its format triple, word bits 64 or 128; vector-unit lanes `L`, `VLMAX`, queue depth; Shared Mem size and banks; Ingest DMA width and ring depth; Job/Resp FIFO depths; A53 cores 1–4. Scheduling: dynamic vs static in-order; real-time overlapped with long-term or not; per-UE or joint CG jobs; long-term tile size. Engine count: 1 built, 2 modeled. |
| Must | Verification: the whole system at RTL at N_rx = 64 (§4 AC9); per-engine calibration builds, ≥ 8 held-out builds, a pre-registered brute-force sub-grid, 2–4 finalists through Vivado implementation. |
| Must | Every figure and table regenerated by one documented command with fixed seeds; a forced re-run of a pre-chosen subset reproduces it byte for byte. |
| Must not | Commit data (Rule 10); read data from outside the repo (Rule 11); open `~/.Xilinx/Xilinx.lic` or any credential file. |
| Must not | Push or merge unasked; change `main`, `feat/sionna-prod` or `paper/cg_dse`. |
| Must not | Change anything under `waveflow/` without a 👁 stop, except the pre-approved edits in §14 (the merge's counters; the `WANT_XSI_GATES` and `WANT_GEM5_GATES` floors raised by the measured counts of this example's new tests; additions-only pre-registration rows in `waveflow/cpu/calib/sweep.py`). Change behaviour of any existing API. |
| Must not | Reformat existing files; overwrite the repo's recorded gate numbers or calibration corpora (new corpora are local; the A53 calibration uses a local platform directory). |
| Nice to have | A comparison against `paper/cg_dse`'s fused streaming engine (what the shared memory costs in time and memory traffic, from that branch's recorded numbers); 16-QAM data; Jacobi preconditioning and warm start from the previous T_LT as accuracy axes; 2 engines of a kind built, not only modeled; separate systolic builds for long-term and real-time jobs, modeled. |
| Constraint | Budgets (each a hard stop for its job; exceeding one stops for the user): accuracy sweeps ≤ 8 h wall clock including their on-the-fly rendering (≤ 4 workers); csynth fit + held-out + brute force ≤ 14 h in one queue (≤ 4 workers, ≤ 2 for N_rx ≥ 256); Vivado implementations ≤ 4; gem5 ≤ 1 h of compute. Disk: signal caches ≤ 30 GB, ≥ 20 GB kept free; build trees removed once their reports are harvested. |
| Constraint | Python 3.12.3 `.venv`; Vitis HLS / Vivado 2024.1 (the only version on this machine; main's 2025.1 vector-unit numbers are not comparable); sionna-rt 2.2.0; gem5 v25.1 via Docker; the repo's `CLAUDE.md`. 8 cores, 31 GB RAM, about 100 GB free disk. |

## 3. Deliverables

| Deliverable | Format | Location | For whom |
|---|---|---|---|
| Branch with the code and tests | git | `paper/ltbf_dse` (committed, not pushed) | the user, the paper |
| The frozen system and algorithm spec | Markdown | `plans/ltbf_dse/ltbf_dse_spec.md` (committed) | the user's review, the doer |
| Signal rendering and front end, channel estimators with op counts, float LTBF chain | Python | `examples/ltbf_dse/` (committed) | the paper, reproducibility |
| Vector programs and the bit-exact fixed-point model of the whole hardware path | Python | `examples/ltbf_dse/` (committed) | the accuracy DSE |
| Engine wrappers, Ingest DMA, Shared Mem system, host and scheduler, harnesses, RTL gates | Python + C++ | `examples/ltbf_dse/hw/` (committed) | the paper |
| A53 kernels (Jacobi, release) and their calibration | C + Python | `examples/ltbf_dse/cpu/` (committed); rows in `waveflow/cpu/calib/sweep.py` (pre-approved) | the paper |
| Cost models, job-level timing simulator, DSE, fidelity, finalists | Python | `examples/ltbf_dse/hw/` (committed) | the paper |
| Scene and drop copies, signal caches, accuracy and cost tables, figures | `.npz`, CSV, PDF/PNG | `examples/ltbf_dse/{data,paper_data,results}/` (**local**) | the paper |
| Findings and figure index | Markdown | `examples/ltbf_dse/paper_data/FINDINGS.md` (**local**) and §16 (committed) | the user |
| Plan and lessons | Markdown | `plans/ltbf_dse/` (committed) | continuity |

## 4. Acceptance criteria

Tier **A** backs the core claim; tier **B** is best effort and cut first when a budget runs out. A
missed target is ⚠️ with the number, never ✅ (Rule 13).

- [ ] **AC0 (A) Branch and hygiene.** `paper/ltbf_dse` contains `b0de5a26` and `193dc4ef`
  (`git merge-base --is-ancestor <sha> HEAD` for both); `git rev-parse main feat/sionna-prod paper/cg_dse`
  = `b0de5a26…`, `193dc4ef…`, `ba757884…`;
  `git ls-files examples/ltbf_dse | grep -E '(^|/)(data|paper_data|results|logs|runs|build|reports|prereg)/|\.(npz|csv|pdf|png)$'`
  prints only the committed pre-registration files listed in §15; `git status -sb` shows no remote
  for the branch. Verify: those commands.
- [ ] **AC1 (A) Merge.** After step 0.2: `pytest tests/rfenv -m "not sionna"` passes;
  `pytest -m sionna -rs` passes with 0 skipped; `pytest tests/build/test_xsi_session_gate.py` passes;
  `WANT_XSI_GATES` equals `pytest -m xsi --collect-only -q`'s count; `CLAUDE.md`'s curated count equals
  `ls docs/examples/*/index.md | wc -l` (expected 28). Verify: those commands.
- [ ] **AC2 (A) Spec frozen.** `plans/ltbf_dse/ltbf_dse_spec.md` covers every item of step 1.1 and was
  approved by the user at the 👁 gate; its SHA-256 is in §15. Verify: §15.
- [ ] **AC3 (A) Signals.** (a) The rendering used equals `rfenv`'s `reference_apply` on a 2×2 array
  of one drop to ≤ 1e-9 relative; (b) with noise off, for every link whose delay spread after timing
  advance is within the CP, the front end's grid on the data REs equals the applier's own frequency
  response (`rfenv.taps` at the sample rate, DFT'd) times `x` to ≤ 1e-3 relative (the interpolator's
  documented floor is about −67 dB); the fraction of links beyond the CP is reported; (c) rendering
  one drop twice gives byte-identical grids. Verify: `pytest tests/examples/test_ltbf_dse_signals.py`.
- [ ] **AC4 (A) Channel estimation with op counts.** (a) The spec derives, for its SRS estimator and
  power convention, the processing gain `G_SRS` it achieves; on a flat synthetic channel the SRS
  estimates' error variance per snapshot is within ±1 dB of that derivation, with no cross-port
  leakage above −30 dB; the achieved `G_SRS` on the drops is reported; (b) the DMRS reduced-domain
  estimate on a noiseless synthetic case equals `Gᵢhᵢ` to ≤ 1e-9; (c) every estimator's logged op
  counts equal the spec's closed-form formulas for at least three shapes. Verify:
  `pytest tests/examples/test_ltbf_dse_chest.py`.
- [ ] **AC5 (A) Float chain.** (a) CG to convergence (`‖rⱼ‖/‖bⱼ‖ < 1e-12`) matches the direct solve
  to ≤ 1e-8 relative in `Aᵢ`, both regimes and both `S` forms; (b) on a synthetic channel with exact
  estimates and `R̃ᵢ` from the true statistics, the measured SINR (`1/mean(MSE) − 1` over the data
  REs) equals `1/mean(1/(1 + γ_RE)) − 1` from the paper's per-RE projected-MMSE SINR (eq. `sinr_proj`)
  within 0.1 dB; (c) the paper check of step 2.4 (genie SINR, like for like with `paper/cg_dse` and the
  paper) is recorded in §15 (not a gate). Verify: `pytest tests/examples/test_ltbf_dse_float.py`; the
  step 2.4 script.
- [ ] **AC6 (A) Bit-exact model.** The fixed-point chain is composed of the engines' own bit-exact
  models (`waveflow.linalg.matmul` for every systolic job with the build's one format triple,
  `waveflow.vecunit.model` running the actual vector programs, the A53 kernel's Python twin) and equals
  each of them job by job on ≥ 20 random problems per job kind, including a zero-divisor case.
  Verify: `pytest tests/examples/test_ltbf_dse_fixed.py`.
- [x] **AC7 (A) Accuracy DSE.** The accuracy grid (§9 b) evaluated with no Vitis in the loop, one
  command, deterministic; `paper_data/accuracy_losses.csv` holds the median and 10th-percentile loss
  per configuration; re-running a pre-chosen subset (2 drops over the whole grid) reproduces its rows
  byte for byte and the figures are byte-stable. Verify: the step 3.2 subset command and `cmp`.
- [ ] **AC8 (A) Wrappers at RTL.** The systolic wrapper, the vector-unit wrapper and the Ingest DMA
  are each bit-exact at RTL (XSI) against their pysim twins on their job sets (every job kind of
  §1.3 at N_rx = 64), with csynth's estimated clock ≤ 4 ns. Verify:
  `pytest -m xsi -rs tests/examples/test_ltbf_dse_xsi.py -k "systolic or vec or ingest"`.
- [ ] **AC9 (A) System at RTL.** Configuration: N_rx = 64, one sector of 10 UEs, factored `Q̂`,
  regime 1, r = 1, t = 3, the spec's smallest tile and ring, the deterministic policy; one long-term
  update then one slot, the Python stages' outputs and the Jacobi results replayed from the pysim
  run's file. Pass when: Shared Mem's final results equal the fixed-point model's and pysim's bit for
  bit; each engine's Job FIFO writes, Ctrl writes and its own done records (as a per-source sequence
  of the Resp FIFO) are identical in pysim and at RTL; pysim's end time is within 5% of RTL's, with
  the C++ host charged the calibrated A53 cycles through `compute(n)`. Verify:
  `pytest -m xsi -rs tests/examples/test_ltbf_dse_xsi.py -k system`.
- [x] **AC10 (A) Scheduler.** Unit tests: every dependency respected; no deadlock at the smallest
  FIFO and ring depths in the space; the Resp FIFO never overflows; the memory plan stays within
  capacity; deadline misses and ring overruns counted correctly on a constructed case. The new release
  kernel is calibrated by a pre-registered gem5 campaign with held-out median cycle error ≤ 10%; the
  Jacobi kernel's calibration from `paper/cg_dse` is re-used and re-checked on 5 held-out inputs.
  Verify: `pytest tests/examples/test_ltbf_dse_sched.py`; `pytest -m gem5 -rs tests/examples/test_ltbf_dse_cpu.py`.
- [ ] **AC11 (A) Cost and time models.** Resource and per-job cycle models of the wrappers and the
  Ingest DMA, from fit builds spanning the target sizes, scored on ≥ 8 held-out builds fixed before
  fitting: median relative error ≤ 10% for cycles and DSP, ≤ 15% for LUT, FF, BRAM, URAM, worst case
  reported. The job-level timing simulator (§9 d) is within 5% of full pysim on these 5
  configurations: N_rx ∈ {64, 256} × {explicit, factored} with the dynamic policy, and N_rx = 64
  factored with the static policy (each one long-term update and one slot); and within 5% of RTL on
  the AC9 configuration. Verify: `python -m examples.ltbf_dse.hw.validate`;
  `paper_data/model_validation.csv`.
- [ ] **AC12 (A) The DSE and its findings.** The joint accuracy × cost × time space explored in
  Python; Pareto frontiers per (array, target, loss budget, T_LT budget); `FINDINGS.md` states in
  numbers, each pointing at its table: (1) the cheapest design per array meeting each loss and time
  budget, and the real-time load it sustains with ingest; (2) explicit vs factored `Q̂`; (3) iterations
  vs word length; (4) where the time goes (engine busy time, Shared Mem and ingest contention, CPU
  dispatch overhead, real-time vs long-term interference). Verify: `python -m examples.ltbf_dse.hw.dse`;
  `FINDINGS.md`.
- [ ] **AC13 (B) Decision fidelity.** On the brute-force sub-grid, pre-registered (SHA-256 in §15)
  before any of its builds runs: a decision is (scenario, loss budget, time budget, cost measure). A
  design's *measured* cost is its csynth resources; its *measured* time is the job-level simulator run
  with every job's cycle count taken from that build (its csynth latency report, corrected by the XSI
  anchors of step 5.4) instead of the fitted models. The model's pick is *right* when its measured time
  ≤ 1.02 × the budget and its measured cost ≤ 1.10 × the least measured cost among designs measured to
  meet both budgets; ≥ 90% right per cost measure. Verify: `python -m examples.ltbf_dse.hw.fidelity`.
- [ ] **AC14 (B) Implemented finalists.** 2–4 finalists through Vivado implementation (2024.1) meet
  4 ns (WNS ≥ 0); post-implementation resources compared with the model. Verify:
  `paper_data/finalists_impl.csv`.
- [ ] **AC15 (A) Regression and lint.** The fast suite shows no failures beyond the step 0.3 baseline;
  `pytest -m sionna -rs` passes; the example's own `-m vitis`, `-m xsi`, `-m gem5` tests pass, and the
  `WANT_XSI_GATES` / `WANT_GEM5_GATES` floors equal the measured collected counts; new files pass
  `ruff check` and `black --check`; changed existing files keep their ruff finding counts from step
  0.3. Verify: §11.

## 5. Context and sources of truth

| Source | What it is | Access | Freshness / version |
|---|---|---|---|
| LTBF paper | The algorithm and its parameters: `longterm.tex` (system model, eqs. `zproj`, `xhat_stage2`, `sinr_proj`), `solution.tex` (SRS estimation eq. `QHprod`, Woodbury remark, CG eqs. `cg_rhs`, `CG_K`–`CG_omega`, `GioptAPP`, signaling §`signalling`), `simulation.tex` (Table "parameters"; `G_SRS` = subcarriers per coherence bandwidth, ≥ 10.8 dB measured, 10 dB used, lines 60–74) | `/home/wirelesslab914/ali/salsa-ltbf/*.tex` (read-only) | repo HEAD `c52662e` |
| Paper code | `draw_srs`, `estimate_Q`, `compute_lt_bf` (`salsa/salsa/salsa_cap.py:329–713`), `compute_sinr_lt` (`:231–302`, the genie SINR), `cg_solve_complex` (`utils.py:557`), `nr_pilots.py` (SRS and DMRS port counts), `srs_budget.py` (`G_SRS` from the coherence bandwidth) | read-only; semantics only, never imported | same HEAD (the `salsa/` tree is untracked there) |
| Previous study | `paper/cg_dse` (`ba757884`): `plans/mimo_cg/ltbf_cg_dse.md` (+ lessons), `examples/ltbf_cg/` | `git show paper/cg_dse:<path>` | 2026-10-14 |
| Local data from that study | 100 drops (`examples/ltbf_cg/data/drops/drop_000NN.{npz,json}`: per sector × UE × path `a0/a1`, `tau0/tau1`, `valid0/valid1`, `k_rx0/k_rx1`, plus positions and velocities), the Denver copy (`data/denver/`), the A53 Jacobi calibration (`data/a53_ltbf/`) | local, untracked (`.git/info/exclude`); copied in step 0.4 | sionna-rt 2.2.0 |
| `waveflow.rfenv` | `PathSet` (`[n_rx_ch, n_tx_ch, n_paths]`), `ChannelApplier`, `reference_apply`, `taps`, `RtSignalSource`, `ArrayWaveform`; `apply.py`'s header (interpolator floor about −67 dB) | `feat/sionna-prod` `193dc4ef`, merged in step 0.2; `docs/guide/rf/rfenv/index.md` | sionna-rt 2.2.0 |
| Systolic engine | `SystolicEngineMem` (`waveflow/linalg/systolic_engine_mem.py`: `EngineJobDesc`, `EngineDone`, `MUL` and `MUL_AH` only, `K/Kmax ≤ Pmax`, formats `a`, `b`, `c` fixed per build (`:311–313`), `MUL` reads A as row-major `Mmax × Kmax` blocks and `MUL_AH` as stored `Kmax × Mmax` blocks (`:505–522`)); `waveflow/linalg/matmul.py` (`acc_format`, ≤ 64 bits); cost `waveflow/linalg/cost.py` | `docs/guide/dsp/systolic/systolic_engine_mem.md` | main `b0de5a26` |
| Vector unit | `VecUnit` (`waveflow/vecunit/unit.py`: stream-only data ports, three program streams, registers and scalars live one job); ISA (`isa.py`: divides only `wf.sdivz`, L lanes; control ops SETVL, QPOP, QPUSH, SETRS: programs are straight-line); `VecUnitConfig` (`VLMAX` < 4096, `Kmax·ceil(Nmax/L)` limits; `scalar_ext` off by default); `hls.py` (programs streamed one instruction at a time); CG program `examples/vecunit_cg/cg_program.py` | `docs/guide/dsp/vecunit/` (`interface.md`, `architecture.md`, `timing.md`) | main; its 2025.1 numbers are from another machine |
| CPU model | `Processor`, `SwFunction`, `CpuPlatform` on `waveflow/calib/platforms/a53_hpi_1200mhz_gem5v25_1/`; calibrated families `sched_ops`, `dispatch`, `ctx_switch` (`waveflow/cpu/calib/kernels/`, rows in `sweep.py`); `examples/cpu_sched/` (dispatchers holding `MMIFMaster`s); calibration `waveflow/cpu/calib/` (`prereg.py` needs rows in `sweep.py`) | `docs/guide/cpu/` | gem5 v25.1 HPI |
| Software host | `waveflow.sw.SwHost` (`waveflow/sw/threads.py:215`) and its C++ twin (`waveflow/build/xsi/xsi_sw.h`, `SwHostModel`, `compute(n)`); the gate compares every host endpoint's trace byte for byte | `docs/guide/build/sw_threads.md` | main |
| Bus system | `bus_system` frame (`docs/guide/ai_tooling/frames.md`); `examples/markov/` (crossbar, `MemoryMod`, `QueueIn`/`QueueOut` with IRQs, `MemWStream`, a host that never polls); `waveflow/hw/mm_device.py` (`QueueIn`, `QueueOut`, `RegBank`, `build_mm_device`); `waveflow/build/system_dag.py` (`add_system_steps`) | `docs/guide/build/xsi_system.md`, `docs/guide/systems/` | main |
| HLS kernel guidance | `docs/guide/vectorization/hls/loop_optimization.md` (required before writing a kernel body) | repo | main |
| Vitis HLS / Vivado | csynth, XSI, implementation | `/tools/Xilinx/{Vitis_HLS,Vivado}/2024.1/` | 2024.1 |

## 6. Environment

- **OS / shell:** Ubuntu 24.04 (Linux 7.0), bash. 8 cores, 31 GB RAM (26 GB available), about 100 GB free disk.
- **Languages and runtimes:** Python 3.12.3 (`.venv`); g++ 13.3.0.
- **Key dependencies:** numpy 2.5.3, scipy 1.18.1, simpy 4.1.2; sionna-rt 2.2.0, Mitsuba 3.9.1, Dr.Jit 1.5.0 (`llvm_ad_mono_polarized`); Vitis HLS / Vivado 2024.1; gem5 v25.1 (`~/ali/tools/gem5/build/ARM/gem5.opt`, image `ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1`), McPAT (`~/ali/tools/mcpat`).
- **Commands:** fast suite `pytest -m "not vitis and not xsi and not gem5 and not sionna" -q -rf`; gates `pytest -m vitis|xsi|gem5|sionna -rs`; lint `ruff check <paths>`, `black --check <paths>`.
- **Project structure:** `waveflow/linalg/`, `waveflow/vecunit/`, `waveflow/cpu/`, `waveflow/sw/`, `waveflow/hw/` (bus, memory, mm devices), `waveflow/rfenv/` (after step 0.2); the new `examples/ltbf_dse/`.
- **Limitations:** no GPU; Vitis/Vivado 2024.1 only; the paper's own environment (sionna-rt 1.2, TensorFlow) is unavailable, so its numbers are a qualitative target; an off-chip memory cannot be wired into the RTL system yet (`plans/offchip_memory.md` is a draft), so DDR is model-only.

## 7. Existing system and reuse

- **Current implementation.** `paper/cg_dse` solved the long-term CG with one fused, example-level
  engine fed by streams, the A53 eigendecomposition afterwards in sequence; no shared memory, no
  real-time path, no signals (channels as frequency responses plus SRS noise). Main has since gained
  `SystolicEngineMem` (the systolic engine behind memory), the programmable `VecUnit`, and the CPU
  model.
- **Copied from `paper/cg_dse` in step 0.5** (`git show paper/cg_dse:<path> > <new path>`, then
  `ltbf_cg` → `ltbf_dse` in imports and paths): `examples/ltbf_cg/scenario.py`, `ltbf_link.py`,
  `ltbf.py`, `ltbf_fixed.py`, `ltbf_eig.py`, `cpu/ltbf_eig.c`, `cpu/sweep_plan.csv` (the committed A53
  pre-registration `ltbf_eig.py` needs), and the tests `tests/examples/test_ltbf_cg.py` and
  `test_ltbf_cg_cpu.py` (as `test_ltbf_dse_link.py` and `test_ltbf_dse_cpu.py`); and
  `waveflow/cpu/calib/sweep.py`'s `ltbf_eig_rows()` (a pre-approved addition, §14). **Not copied:**
  `examples/ltbf_cg/hw/*` imports the old engine (`engine`, `space`, `timing`, `build`, `host_check`);
  read those files with `git show` as references when writing `hw/models.py`, `hw/dse.py`,
  `hw/fidelity.py`, `hw/finalists.py`, `hw/queue.py` and `hw/validate.py`.
- **Reuse from main, unchanged:** `SystolicEngineMem` and its cost model; `VecUnit`, its assembler,
  interpreter and calibrated timing; `Processor`, `SwFunction`, the A53 platform and its `sched_ops`,
  `dispatch` and `ctx_switch` families; `SwHost` and its C++ twin; `MemoryMod`, the crossbar,
  `build_mm_device` views, `MemRStream`/`MemWStream`; the system DAG and XSI flow; `SweepRunner`;
  `waveflow.events` for cost accounting.
- **Conventions.** `CLAUDE.md`; `plans/source_layout.md` (generated `include/`, `gen/`, `xsi/`,
  `*_proj/` untracked; authored C++ in `src/` or `hw/cpp/`); command-response protocol
  (`docs/guide/systems/command_response.md`); HLS bodies follow
  `docs/guide/vectorization/hls/loop_optimization.md`; never pack words by hand.
- **Docs to keep in sync:** `CLAUDE.md`'s curated-example count (step 0.2). The example is not
  curated; no guide page is required. Step 7.2 proposes any `CLAUDE.md` or guide additions to the user.

## 8. Tools and access (readiness)

Checked 2026-10-28. Re-run before starting:

```bash
# guided-task plugin's readiness script (run via marketplace_env, see its SKILL.md):
export MARKETPLACE_ROOT=/home/wirelesslab914/ali/claude-marketplace
. "$MARKETPLACE_ROOT/common/scripts/skill_env.sh"
READY="$MARKETPLACE_ROOT/common/plugins/guided-task/skills/guided-task/scripts/check_readiness.py"
"$PY" "$READY" --tool git --tool g++ --tool docker \
  --path /tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls --path /tools/Xilinx/Vivado/2024.1/bin/vivado \
  --path .venv/bin/python \
  --path /home/wirelesslab914/ali/salsa-ltbf/solution.tex \
  --path /home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/salsa_cap.py \
  --path examples/ltbf_cg/data/drops --path examples/ltbf_cg/data/denver/denver.xml \
  --path /home/wirelesslab914/ali/tools/gem5/build/ARM/gem5.opt --path /home/wirelesslab914/ali/tools/mcpat \
  --writable plans/ltbf_dse --writable examples --git .
# Read-only probes:
source .venv/bin/activate
/tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls -version | grep -m1 v2024
/tools/Xilinx/Vivado/2024.1/bin/vivado -version | grep -m1 -i "vivado v"
python -c "import sionna.rt, mitsuba as mi, importlib.metadata as m; print('sionna-rt', m.version('sionna-rt'), mi.variant())"
docker image inspect ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1 --format '{{.Id}}'
for r in main feat/sionna-prod paper/cg_dse; do git rev-parse --short $r; done
git merge-tree --write-tree --name-only main feat/sionna-prod   # expect conflicts in CLAUDE.md, tests/conftest.py only
nproc; free -g; df -h /home/wirelesslab914
```

| Check | Target | Status | Detail |
|---|---|---|---|
| tool | `git` | ✅ ready | git version 2.43.0 (/usr/bin/git) |
| tool | `g++` | ✅ ready | g++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0 (/usr/bin/g++) |
| tool | `docker` | ✅ ready | Docker version 29.4.1, build 055a478 (/usr/bin/docker) |
| path | `/tools/Xilinx/Vitis_HLS/2024.1/bin/vitis_hls` | ✅ ready | file, readable |
| path | `/tools/Xilinx/Vivado/2024.1/bin/vivado` | ✅ ready | file, readable |
| path | `.venv/bin/python` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/salsa-ltbf/solution.tex` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/salsa-ltbf/salsa/salsa/salsa_cap.py` | ✅ ready | file, readable |
| path | `examples/ltbf_cg/data/drops` | ✅ ready | directory, readable |
| path | `examples/ltbf_cg/data/denver/denver.xml` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/tools/gem5/build/ARM/gem5.opt` | ✅ ready | file, readable |
| path | `/home/wirelesslab914/ali/tools/mcpat` | ✅ ready | directory, readable |
| writable | `plans/ltbf_dse` | ✅ ready | does not exist yet — will be created (parent is writable) |
| writable | `examples` | ✅ ready | directory, writable |
| git | `.` | ✅ ready | branch `main`, working tree clean |

**Readiness: READY** — 15 ready · 0 to check · 0 blocked

Probes (2026-10-28): Vitis HLS v2024.1; Vivado v2024.1; sionna-rt 2.2.0, `llvm_ad_mono_polarized`;
gem5 image `sha256:a06a11d9371d…`; `main` = `origin/main` = `b0de5a26`, `feat/sionna-prod` =
`origin/feat/sionna-prod` = `193dc4ef`, `paper/cg_dse` = `ba757884`; the dry-run merge conflicts in
`CLAUDE.md` (curated count, 27 vs 25; 28 after the merge) and `tests/conftest.py` (`WANT_XSI_GATES`
245 vs 239) only; 100 drops (2.6 MB) and the Denver copy (12 MB) present; 8 cores, 31 GB RAM
(26 GB available), 100 GB free.

| Capability | Kind | Purpose | Available? |
|---|---|---|---|
| Explore / general-purpose subagents | agent | code reading; background reviews | yes |
| Monitor (ScheduleWakeup if available) | harness tools | polling detached jobs (Rule 6) | yes |
| `waveflow` MCP server | MCP | guide lookups, `waveflow_get_process("bus_system")` | yes (not required) |
| The user | person | the 👁 spec gate, unexpected conflicts, budget overruns, framework changes | yes |

**Gaps the user accepted:** none.

## 9. Approach

**Chosen approach.** Merge, baseline and copy first; then write and freeze the spec (one 👁 stop),
with spikes that de-risk the unknowns before it is frozen; then the signal-level float chain, the
vector programs and the bit-exact model (accuracy is ready early and needs no Vitis); then the
wrappers, the Ingest DMA, the host and scheduler and the system in pysim, then at RTL; then
calibration, the job-level timing simulator and the DSE. Everything long is detached and resumable.

**(a) Signals.** `examples/ltbf_dse/signals.py` builds each UE's waveforms (SRS symbol at snapshot 0;
PUSCH slot with DMRS and QPSK at snapshot 1) per the spec, as `rfenv` `ArrayWaveform`s; each drop's
paths become `PathSet`s per sector (the synthetic array applied as `paper/cg_dse`'s `scenario.py`
does), with each UE timing-advanced to its serving sector's earliest path. Rendering goes through
`rfenv`; if spike 1.2 a shows it too slow for on-the-fly use at 32×32, the example uses a factored
fast path (each path's delay is shared across a sector's antennas, so the received samples are
`A · S_delayed`, antennas × (UEs·paths) times (UEs·paths) × samples), checked against
`reference_apply` (AC3 a). `frontend.py` removes the CP and FFTs each antenna into grid tiles.
**SRS:** 30 ports on one symbol across 4 combs (about 8 cyclic shifts per comb), observed by every
sector; the estimator separates the ports in the delay domain (per comb: multiply by the conjugate
base sequence, IDFT, window each port's cyclic-shift region, DFT back), then forms `N_SRS` = 8
snapshots spread over the band. The spec states the power convention (comb concentration) and
derives the `G_SRS` this achieves (AC4 a); the paper's 10 dB is a reference point, not a target.
**DMRS:** NR type-2 double-symbol ports orthogonal within a sector (the paper's Table "pilot");
other sectors' UEs are not orthogonal (pilot contamination, realistic). The spec fixes the details.

**(b) Accuracy grid (no Vitis).** The reference is exact float LTBF on the same signals and
estimates. One 10-iteration run per configuration gives every t ≤ 10 as its prefix; r = 1 and 2 come
from the same `Aᵢ`. Axes: target, array, word width and guard bits (capped by `acc_format`), the
systolic format triple, `Q̂` form, regime, r. The fixed-point chain is built from the engines' own
models (AC6), so it is what the hardware computes, including the Shared Mem formats between jobs.
Runner: the `SweepRunner` pattern, detached, ≤ 4 workers, rendering on the fly.

**(c) Hardware.** Example-level, each a `FreeRunMod` composite with `mm_views`:
- **Systolic wrapper:** Job FIFO (`QueueIn`), thin Ctrl (`RegBank`), a job translator from the
  spec's job descriptor to `EngineJobDesc`, its done record; `SystolicEngineMem` unchanged, one format
  triple per build.
- **Vector-unit wrapper:** Job FIFO, Ctrl (program table, status), and a memory front end with **one
  reader per stream** (`s_lprog`, `s_xprog`, `s_sprog` and each data port get their own `MemRStream`,
  so no stream waits behind another; a single shared reader deadlocks) and a writer (`MemWStream`)
  that turns each `VecResp` and its data into a memory write; the job's `VecStatus` becomes its done
  record. Programs are straight-line (control ops are only SETVL, QPOP, QPUSH, SETRS) and stream one
  instruction at a time, so their length and their Shared Mem traffic grow with the data; spike 1.2 c
  sizes them. Registers and scalars live one job, so the CG step loads `X, R, D, U` every iteration and
  recomputes its scalars with `wf.vdotr` (scalars can't be loaded from memory with `scalar_ext` off,
  the calibrated default); a dot product over N_rx needs one block with `k = VL = N_rx`, within
  `Kmax·ceil(Nmax/L) ≤ 4096`. The equalizer program divides with `wf.sdivz` (L per instruction, at
  VL = 1); its RE rate is measured in the spike.
- **Ingest DMA:** a `FreeRunMod` fed by a stream of grid tiles (in pysim from the front end; at RTL
  replayed from a file), writing each tile into the next ring slot through a `MemWStream` and pushing a
  tile-ready record; the ring depth and port width are knobs.
- **Layout contract** (spec item v): every systolic operand in Shared Mem is block-major as
  `SystolicEngineMem` reads it, and `MUL` reads A as row-major `Mmax × Kmax` blocks while `MUL_AH`
  reads the stored `Kmax × Mmax` blocks, so `Ĥα` is stored **twice** (once per orientation) in both
  `Q̂` forms, written by the Python estimator and counted in the working set; `Ĥαᴴ` too when `Q̂` is
  explicit (no `A·Bᴴ`). A vector block read as a row-major `k × n` burst equals the systolic layout
  only when the matrix is one systolic tile wide (`N = Nmax`); the spec fixes the vector unit's blocks
  to that, or states the conversion. `r`, `M` and `N` are padded to `Nmax`, `Mmax`.

**(d) Timing for the DSE.** Full pysim moves data and is bit-exact but slow at LTBF sizes. The DSE
uses a **job-level timing simulator** (`hw/jobsim.py`): the same scheduler code and job graphs, the
Ingest DMA's tile arrivals at line rate, each job's duration from its calibrated per-job model,
Shared Mem port contention from the bus model, no data. It is validated against full pysim and RTL
(AC11). Sizes whose working set exceeds the on-chip Shared Mem are priced with a stated DDR model
(`DDR4-2400 x64 at 70%`, as `paper/cg_dse`) and flagged *model-only*.

**(e) Host and RTL.** In pysim the host is a `SwHost` whose threads run the scheduler and submit
their compute to the `Processor`; spike 1.2 e builds the smallest version of this (one thread, one
engine job, one IRQ) and its C++ twin before the spec is frozen. At RTL the C++ twin runs the same
scheduler with a deterministic policy (static order with dependency waits), charges each operation's
calibrated A53 cycles through `compute(n)`, replays the Python stages' outputs and links
`ltbf_eig.c` for the eigendecomposition. The Resp FIFO receives records in completion order, which
depends on timing, so the framework's byte-for-byte per-endpoint host gate is not applied to it as
is: the example's gate (AC9) compares each source's subsequence of Resp records, the Job FIFO and
Ctrl writes per engine, and the final Shared Mem contents.

**(f) Findings** (§4 AC12) and, as nice-to-have, the comparison with `paper/cg_dse`'s fused engine.

**Search before building:** `waveflow.linalg.SystolicEngineMem`, `waveflow.vecunit` (programs,
interpreter, timing), `waveflow.cpu` and `examples/cpu_sched`, `waveflow.sw.SwHost`,
`waveflow.hw.mm_device` and `examples/markov`, `waveflow.rfenv`, and the whole `paper/cg_dse`
example. New code: the signal builder and front end, the estimators and op counter, the vector
programs, the wrappers, the Ingest DMA, the host and scheduler, the job-level simulator, and the glue.

Alternatives considered:

| Option | Pros | Cons | Fits? |
|---|---|---|---|
| Wrappers at example level around unchanged engines (chosen) | No framework diff; fast; upstreaming later | Duplicated wrapper logic if another app needs it; systolic formats fixed per build | ✅ user's choice |
| Wrappers in `waveflow/vecunit` and `waveflow/linalg`; run-time systolic formats | Reusable; Ctrl could switch formats | Touches another owner's packages, docs and gates | ❌ user chose example-level and a thin systolic Ctrl |
| Keep CG state in the vector unit across iterations (one job per solve, `vecunit_cg` style) | Less memory traffic | Breaks "every hand-off through Shared Mem" | ❌ violates §1.2 |
| Free ingest (Python writes Y at no bus cost) | Simplest | Overstates real-time capacity (22.7 GB/s at 64 antennas vs about 4 GB/s per 128-bit port) | ❌ user chose the Ingest DMA |
| Channel estimation in hardware | A complete receiver | Larger scope; the user wants it in Python, with op counts | ❌ user |
| Time-domain samples into hardware with an FFT per antenna | End-to-end hardware | FFTs dominate resources at 64+ antennas | ❌ user chose the grid |
| Store every rendered grid | Rendering once | About 77 GB | ❌ on the fly instead |
| Re-trace drops with per-path Doppler | Exact in-slot Doppler | About 8 h of tracing | ❌ user chose reuse |
| Full pysim as the DSE's time model | One model | Too slow for the DSE's size | ❌; used to validate the job-level simulator |

## 10. Steps

Milestones: M0 = Phase 0 · M1 = Phase 1 · M2 = Phase 2 · M3 = Phase 3 · M4 = Phase 4 · M5 = Phase 5 ·
M6 = Phase 6 · close-out = Phase 7. Milestones do not pause (Rule 5); run `git status -sb` at each.

Flags: ⚠️ irreversible (none planned) · 👁 the user reviews before the work continues · ⏱ a budgeted,
detached compute job (Rules 5, 6).

### Phase 0 — Setup and the merge (M0)

| # | Step | Inputs | Exit condition (verifiable) | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 0.1 | Create `paper/ltbf_dse` from `main` (`b0de5a26`); create the lessons file; commit this plan (already in the working tree, untracked) and the lessons file | this plan | On the branch; `main` unchanged | `git branch --show-current`; `git rev-parse main` | commit | ☑ |
| 0.2 | `git merge --no-ff --no-commit feat/sionna-prod`. **Pre-approved:** in `CLAUDE.md` set the curated count to `ls docs/examples/*/index.md \| wc -l` (expected 28, in words as the line writes it); in `tests/conftest.py` keep both histories' comment lines, add a line for this merge, and set `WANT_XSI_GATES` to `pytest -m xsi --collect-only -q`'s count (expected 245 + 1 = 246; record the measured number); accept the root `.gitignore` lines the merge brings. **Any other conflict: stop and ask.** Then run AC1's commands; commit | `feat/sionna-prod` | AC1 | AC1's commands | commit (merge) | ☑ |
| 0.3 | Baseline, before any other edit: the fast suite, detached (about 8 min); record its failures and the ruff finding counts of every existing file this plan may touch (`tests/conftest.py`, `waveflow/cpu/calib/sweep.py`) | 0.2 | Recorded in §15 | the fast-suite log; `ruff check <file> \| tail -1` | — | ☑ |
| 0.4 | Create `examples/ltbf_dse/.gitignore` (`data/`, `paper_data/`, `results/`, `logs/`, `runs/`, `gen/`, `include/`, `xsi/`, `*_proj/`, `hw/build/`, `hw/prereg/`, `hw/reports/`, `cache/`); copy `examples/ltbf_cg/data/{denver,drops,a53_ltbf}` into `examples/ltbf_dse/data/`; record the copies' SHA-256 manifest in §15 | local data | `git check-ignore -v examples/ltbf_dse/data/drops/drop_00001.npz` matches; `git status --short examples/ltbf_dse` shows only `.gitignore`; manifest recorded | the commands | commit | ☑ |
| 0.5 | Copy the `paper/cg_dse` files listed in §7 into `examples/ltbf_dse/` and `tests/examples/` (renamed), and add `ltbf_eig_rows()` to `waveflow/cpu/calib/sweep.py` (pre-approved, additions only); the copied tests pass unchanged in meaning | `paper/cg_dse` | The copied fast tests pass; the `-m gem5` A53 test passes | `pytest tests/examples/test_ltbf_dse_link.py tests/examples/test_ltbf_dse_cpu.py -m "not vitis and not xsi and not gem5"`; `pytest -m gem5 -rs tests/examples/test_ltbf_dse_cpu.py` | commit | ☑ |

### Phase 1 — Spec and spikes (M1)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 1.1 | Write `plans/ltbf_dse/ltbf_dse_spec.md`: (i) slot structure, timing advance, SRS (combs, cyclic shifts, the delay-domain estimator, the 8 snapshots, the power convention and the derived `G_SRS`), DMRS ports, QPSK, power control, noise; (ii) the estimators and their op-count formulas; (iii) the long-term and real-time algorithms of §1.1 with every choice made (the `R̃ᵢ` source, the symmetrization, the stop rule, the zero guard); (iv) fixed-point formats per buffer and job, with the systolic engine's one triple per build and the word-width cap; (v) the system: address map, every buffer's layout (§9 c's contract, including `Ĥα`'s two copies and padding), the Job FIFO descriptors per engine (`DataList`s), the Ctrl register maps (thin systolic, vector-unit program table), the done and tile-ready records, the Resp path (direct streams by default), FIFO and ring depths, the Ingest DMA's tile shape; (vi) the job graphs (long-term per sector, real-time per tile); (vii) the scheduler of §1.4; (viii) the host pattern of §9 e; (ix) the architecture diagram of §1.2, updated by the spikes | §1, the paper, 1.2 | Every item present; open choices listed with a recommendation | read-through | commit | ☑ |
| 1.2 | **Spikes**, each recorded in §15 and §14: (a) rendering time and memory for one drop at 32×32 (1 SRS symbol + 1 slot, 3 sectors), through `rfenv` and through the factored fast path; the delay-spread statistics after timing advance; (b) `SystolicEngineMem` at LTBF shapes: `Pmax` for K = 1024, the widest format `acc_format` accepts at K = 240 and 1024, csynth at one mid configuration (time, memory, clock); (c) the vector unit on its interpreter: the per-iteration CG step program and the equalizer program, their instruction counts and program bytes per job at N_rx 64/256/1024, `VLMAX` and `Kmax·ceil(Nmax/L)` against N_rx, the RE rate with `wf.sdivz`, and the one-reader-per-stream front end on a toy job; (d) Shared Mem: the working set per array size (with `Ĥα`'s two copies), the ingest bandwidth per array against the bus port, the ring size, and which sizes need the DDR model; (e) the host: a `SwHost` thread charging a `Processor`, writing one job into a toy engine's `QueueIn` and sleeping on a `QueueOut` IRQ, in pysim and with its C++ twin under XSI, `compute(n)` charged | 1.1 draft | Each spike's numbers and its consequence for the spec recorded; any step of §10 that becomes impossible → stop (Rule 5 f) | the spike scripts' outputs; spike e: its XSI test | commit | ☑ |
| 1.3 | Background reviewer agent on the spec (read-only) | 1.1, 1.2 | Findings recorded | — | — | ☑ |
| 1.4 👁 | **The user reviews the frozen spec** (diagram, address map, job formats, registers, scheduler, host pattern, algorithm choices) with the reviewer's findings; apply the changes; record its SHA-256 | 1.1–1.3 | AC2 | §15 | commit | ☑ |

### Phase 2 — Signals, estimation and the float chain (M2)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 2.1 | `examples/ltbf_dse/signals.py` (UE waveforms, `PathSet`s per sector with timing advance, rendering, the fast path if needed) and `frontend.py` (CP removal, FFT, grid tiles); a bounded cache under `examples/ltbf_dse/cache/`; tests | spec | AC3 | `pytest tests/examples/test_ltbf_dse_signals.py` | commit | ☑ |
| 2.2 | `opcount.py` (the op-counting helper) and `chest.py` (SRS → `Ĥⱼ`, `Ĥα` in its two stored layouts, `Ĥαᴴ`; DMRS → `h̃ᵢ[n,k]`); tests | 2.1 | AC4 | `pytest tests/examples/test_ltbf_dse_chest.py` | commit | ☑ |
| 2.3 | `ltbf_chain.py`: the float chain (exact and CG long term; `R̃ᵢ`; projection; DMRS estimation; MMSE; measured SINR, EVM, BER; the paper's genie SINR) on top of the copied `ltbf.py`; tests | 2.2 | AC5 (a), (b) | `pytest tests/examples/test_ltbf_dse_float.py` | commit | ☑ |
| 2.4 | The paper check on 10 drops: exact LTBF vs instantaneous MMSE and t = 3, genie SINR, noiseless and nominal SRS, compared with `paper/cg_dse`'s AC2 (c) numbers (2.38 / 6.27 / 10.71 dB nominal-SRS loss at 8×8 / 16×16 / 32×32) and the paper's; plus the realizable receiver's measured SINR on the same drops; recorded in §15 | 2.3 | Recorded (AC5 c) | `python -m examples.ltbf_dse.ltbf_paper_check` | commit | ☑ |

### Phase 3 — Vector programs, the bit-exact model and the accuracy DSE (M3; overlaps Phase 4)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 3.1 | `hw/programs.py` (the CG step and equalizer programs, assembled with `waveflow.vecunit.asm`, from spike 1.2 c) and `ltbf_fixed_chain.py`: the fixed-point chain composed from `waveflow.linalg.matmul` (every systolic job, block-major, one format triple), `waveflow.vecunit.model` running those programs, and the A53 kernel's twin; the formats dataclass; tests | 2.3, spec | AC6 | `pytest tests/examples/test_ltbf_dse_fixed.py` | commit | ☑ |
| 3.2 ⏱ | `ltbf_accuracy_sweep.py` (§9 b): a 1% pilot sets the run time, the grid trimmed to ≤ 8 h (trim recorded in §14); run **detached**, ≤ 4 workers, rendering on the fly; merge into `paper_data/accuracy_losses.csv`; re-run the pre-chosen subset and `cmp` | 3.1, 2.1 | AC7 | the subset re-run + `cmp` | commit (code only) | ☑ |
| 3.3 | Accuracy figures (loss vs t × width per array; explicit vs factored; regime; r) and candidate findings (3) in §14 | 3.2 | Figures exist | `python -m examples.ltbf_dse.ltbf_accuracy_figures` | commit | ☑ |

### Phase 4 — Wrappers, host and the system in pysim (M4; starts after 1.4)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 4.1 | `hw/sys_wrap.py`: the systolic wrapper (§9 c) around `SystolicEngineMem`; pysim tests against `matmul` on every systolic job kind | spec | Tests pass | `pytest tests/examples/test_ltbf_dse_hw.py -k systolic` | commit | ☐ |
| 4.2 | `hw/vec_wrap.py`: the vector unit's wrapper (§9 c) around `VecUnit`, running `hw/programs.py`; pysim tests against `waveflow.vecunit.model` | spec, 3.1 | Tests pass | `pytest tests/examples/test_ltbf_dse_hw.py -k vec` | commit | ☑ |
| 4.3 | `hw/ingest.py`: the Ingest DMA (§9 c); pysim tests (tiles land at the ring's addresses, records pushed, overrun counted) | spec | Tests pass | `pytest tests/examples/test_ltbf_dse_hw.py -k ingest` | commit | ☑ |
| 4.4 | `hw/sched.py` (the scheduler of §1.4: graphs, list dispatch, priorities, memory plan, flow control, errors; dynamic and static policies) and `hw/graphs.py` (long-term and real-time graphs); unit tests | spec | AC10 (unit tests) | `pytest tests/examples/test_ltbf_dse_sched.py` | commit | ☑ |
| 4.5 | `hw/system.py`: the `HwSystem` of §1.2 (the `SwHost` + `Processor` host of §9 e running the scheduler, crossbar, Shared Mem, both wrappers, the Ingest DMA, Resp FIFO, the Python estimators as zero-time stages); the AC9 configuration equal to the fixed-point chain bit for bit; it writes the replay file the RTL host needs | 4.1–4.4, 3.1 | Test passes | `pytest tests/examples/test_ltbf_dse_hw.py -k system` | commit | ☑ |
| 4.6 | The A53 release kernel (C kernel, Python twin, `Kernel` entry and `Family`), pre-registration rows in `waveflow/cpu/calib/sweep.py` (pre-approved, additions only), the gem5 campaign (≤ 1 h compute) in a local platform directory; Jacobi re-checked on 5 held-out inputs; raise `WANT_GEM5_GATES` by the measured count of new `gem5` tests (pre-approved) | 4.4 | AC10 (calibration) | `pytest -m gem5 -rs tests/examples/test_ltbf_dse_cpu.py`; `pytest -m gem5 --collect-only -q` | commit | ☑ |
| 4.7 | Background reviewer agent on M2–M4 (read-only) | — | Findings recorded; dispositions in §14 | — | — | ☑ |

### Phase 5 — RTL, calibration and validation (M5)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 5.1 | Each wrapper's and the Ingest DMA's csynth and XSI at N_rx = 64 on the system DAG (`add_system_steps`); RTL = pysim on its job set | 4.1–4.3 | AC8 | `pytest -m xsi -rs tests/examples/test_ltbf_dse_xsi.py -k "systolic or vec or ingest"` | commit | ☐ |
| 5.2 | The system at RTL: the C++ host twin (§9 e), the system top, XSI of the AC9 configuration; raise `WANT_XSI_GATES` by the measured count of the example's new `xsi` tests (pre-approved) | 4.5, 5.1 | AC9 | `pytest -m xsi -rs tests/examples/test_ltbf_dse_xsi.py -k system`; `pytest -m xsi --collect-only -q` | commit | ☐ |
| 5.3 | `hw/space.py` (the knob space of §2) and **pre-registration** of the fit / held-out split (≥ 8 held-out) and the brute-force sub-grid with its decision rule (AC13), sized from the measured build rate to fit the queue budget; files local, SHA-256 committed in §15 | 5.1 | Hashes committed | — | commit | ☐ |
| 5.4 ⏱ | The build queue **detached** (Rule 6): fit → held-out → brute force, ≤ 4 workers (≤ 2 at N_rx ≥ 256, 10 GB watchdog), resumable, reports harvested and build trees removed as they finish; XSI anchors for the per-job cycle models | 5.3 | Builds merged into local tables, or the cut recorded at the budget | the queue's summary | commit (code only) | ☐ |
| 5.5 | `hw/models.py` (resource and per-job cycle models; the DDR model stated) and `hw/jobsim.py` (the job-level timing simulator, §9 d); freeze the models (hash in §15) before reading the held-out builds; `hw/validate.py` | 5.4 (fit), 4.5 | AC11 | `python -m examples.ltbf_dse.hw.validate` | commit | ☐ |

### Phase 6 — The DSE, fidelity and finalists (M6)

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 6.1 | `hw/dse.py`: join accuracy × cost × time (long-term within the T_LT budgets, real-time sustained rate with ingest); frontiers; `hw/findings.py` writes `FINDINGS.md` (findings 1–4) | 3.2, 5.5 | AC12 | `python -m examples.ltbf_dse.hw.dse` | commit | ☐ |
| 6.2 | `hw/fidelity.py`: score decision fidelity on the brute-force builds (AC13's measured cost and time) | 5.4, 6.1 | AC13, or the shortfall with its cause | `python -m examples.ltbf_dse.hw.fidelity` | commit | ☐ |
| 6.3 ⏱ | Implement 2–4 finalists (Vivado 2024.1) | 6.1 | AC14, or the cut recorded | `paper_data/finalists_impl.csv` | commit | ☐ |

### Phase 7 — Close-out

| # | Step | Inputs | Exit condition | Verify with | Checkpoint | Status |
|---|---|---|---|---|---|---|
| 7.1 | Regression (§11); figure index; `FINDINGS.md` final | all | AC15 | §11 commands | commit | ☐ |
| 7.2 | Final background reviewer agent; act on its findings with the user; propose to the user any `CLAUDE.md` or guide additions this work suggests (for example the `SwHost` + `Processor` host pattern), applied only on their yes; §16; status | all | §16 written; AC0 re-checked | AC0's commands | commit | ☐ |

## 11. Verification strategy

- **Per step:** the step's *Verify with*. Signals against `rfenv`'s reference and its own frequency
  response; estimators against their derivations and op-count formulas; the float chain against
  direct solves and the paper's SINR formula; the fixed-point chain against the engines' own models;
  the wrappers and the Ingest DMA against their models in pysim and at RTL; the system against the
  fixed-point chain in pysim and at RTL; the cost and time models against held-out builds, full pysim
  and RTL; the DSE's decisions against brute force.
- **Regression check:** `pytest -m "not vitis and not xsi and not gem5 and not sionna" -q -rf` (no
  failures beyond the step 0.3 baseline); `pytest -m sionna -rs`; the example's own
  `pytest -m vitis -rs tests/examples/test_ltbf_dse*.py`, `-m xsi`, `-m gem5`; the gate floors equal
  the collected counts; `ruff check` and `black --check` on `examples/ltbf_dse` and
  `tests/examples/test_ltbf_dse*.py`; the ruff counts of `tests/conftest.py` and
  `waveflow/cpu/calib/sweep.py` unchanged from step 0.3.
- **Independent review:** a read-only `general-purpose` reviewer agent ran on this plan before
  approval (§14); then at steps 1.3, 4.7 and 7.2, in the background (Rule 5); findings and
  dispositions in §14.

## 12. Safety

- **Version control:** branch `paper/ltbf_dse`; commit per step; nothing pushed; `git status -sb` at
  each milestone (Rule 9).
- **Diff review by the user:** at the 👁 spec gate (step 1.4), and on any stop under Rule 5.
- **Irreversible or costly actions:**

  | Action | Step | Backup / recovery | Dry run | Approval |
  |---|---|---|---|---|
  | Merge `feat/sionna-prod` | 0.2 | `git merge --abort`; the branch is new | `git merge-tree` lists the conflicts first (§8) | pre-approved for the two known conflicts; any other: at the time |
  | A compute job beyond its budget in §2 | any ⏱ | stop the job; finished items stay usable (resumable) | a pilot or spike first | at the time |
  | Removing harvested build trees | 5.4 | the reports are copied first; trees are regenerable | — | covered by this plan (local build output only) |

- **Permissions:** read-only outside the repo (the paper repo, the AMD tools, gem5); writes inside
  the repo; Docker for gem5 only.
- **Secrets:** none.
- **Deviation policy:** Rule 4 (2 hypotheses, then stop) and Rule 5 (stops only when required; a
  budget overrun stops).

## 13. Agents and models

| Role | Agent / model | Responsibility |
|---|---|---|
| Doer | Claude Opus 5.5 (this session or a fresh one) | every step |
| Reviewer | `general-purpose` subagent, read-only | the plan before approval (done); steps 1.3, 4.7, 7.2, in the background |
| Explorer | `Explore` subagent | optional, for reading `paper/cg_dse` or framework code quickly |

## 14. Decisions, assumptions, risks, open questions

| Kind | Item | Resolution / owner |
|---|---|---|
| Decision | Level L3; pauses only when required; independent review of the plan and at milestones | user, 2026-10-28 |
| Decision | Architecture of §1.2: per engine a Job FIFO written by the CPU and a Ctrl register space for configuration; one Resp FIFO at the CPU; every hand-off through Shared Mem | user, 2026-10-28 |
| Decision | An **Ingest DMA** master added to the architecture: the grid enters Shared Mem in tiles with real bus cost, tile-ready records into the Resp FIFO (after the plan review found ingest missing) | user, 2026-10-28 |
| Decision | **Thin systolic Ctrl** (enable, status, counters, job defaults); its format triple fixed per build and a DSE knob; the vector unit's Ctrl holds the program table | user, 2026-10-28 |
| Decision | Hardware input is the frequency-domain grid from a Python front end (O-RAN 7.2x); channel estimation in Python with op counts for later complexity calculations; projection and equalization in hardware | user, 2026-10-28 |
| Decision | Each UE timing-advanced to its serving sector; delay spread beyond the CP kept as real ISI and reported | user, 2026-10-28 |
| Decision | Shared Mem on chip; sizes that don't fit priced by a DDR model, flagged model-only | user, 2026-10-28 |
| Decision | Branch from `main`; merge `feat/sionna-prod`; copy reusable `paper/cg_dse` code (the list of §7) into `examples/ltbf_dse/`; reuse the 100 local drops (no re-tracing) | user, 2026-10-28 |
| Decision | Wrappers (Job FIFO, Ctrl, Resp path, the vector unit's memory front end, the Ingest DMA) at example level; engines unchanged | user, 2026-10-28 |
| Decision | Accepted as suggested: the claim (§1), no hard deadline with the budgets of §2, the DSE axes, the metrics and budgets, the verification set, the paper check as a report, the confirmations of the interview | user, 2026-10-28 |
| Decision | The scheduler (§1.4) is supporting infrastructure, not a contribution of the paper: one dynamic policy built, static in-order as the baseline, its new kernel calibrated on gem5 | user, 2026-10-28 |
| Decision (pre-approved) | Framework edits allowed without a stop: the merge's counter lines (`CLAUDE.md`, `tests/conftest.py`); the `WANT_XSI_GATES` and `WANT_GEM5_GATES` floors raised by the measured counts of this example's new tests; additions-only pre-registration rows in `waveflow/cpu/calib/sweep.py` | user, 2026-10-28 |
| Decision | Rendering on the fly (caches ≤ 30 GB) instead of storing every grid (about 77 GB) | plan review, 2026-10-28; confirm at plan approval |
| Decision | **Vitis HLS 2024.1 crashes on `SystolicEngineMem`** (spike 1.2 b: a segfault when HLS makes the core's `STEP` pipeline free-running; the engine was only built with 2025.1). Every systolic build of this example adds `config_compile -pipeline_style flp` through `render_tcl(..., solution_config=...)`, the engine source unchanged; its RTL is checked bit-exact against pysim at XSI before anything is measured, and every systolic number is labelled **2024.1+flp** | user, 2026-10-28 |
| Decision | **Framework fix:** `waveflow/toolchain/toolchain.py` `find_vitis_include_dir` falls back to the sibling `Vitis_HLS/<version>/include` when the Vitis tree has no `ap_int.h` (2024.1's split install), with three unit tests in `tests/toolchain/test_toolchain.py`. Without it every C++-software-host XSI gate (markov, mm_fir, AC9) fails on this machine (spike 1.2 e) | user, 2026-10-28 |
| Decision | The spec frozen at v2 (SHA-256 in §15): the layout contract (`Kmax = Nmax = c`, `Mmax` free, `W = ĤαᴴX`), the Ingest DMA's corner-turn buffer and chunk ring, three program readers and affine descriptors for the vector unit, Cholesky whitening in `G'` on the CPU, the equalizer's weights from the Python estimator and their per-RE application on the vector unit, SRS on 64 RBs (`G_SRS` 16.14 dB), 16-bit lanes with 32 as an axis | user, 2026-10-28 |
| Decision | **Spec amendment 1** (spec §4.1): `X` keeps its own format, the last CG iteration stores `X_b = q_bc(2^-m X)` for `W`; the ingest's gain `2^-k` on `Y`, the CPU's `2^-g` on `G'`, the estimator's `2^(g+k)` back; the CPU removes `2^(s+m)` from `W`. New spec SHA-256 `73eaad3143dc919c7694eb195d9f36c5ff75be65dce8800358f81b082e45e4d2` | user, 2026-10-28 (step 3.1) |
| Decision | **The system's bus is 64 bits wide** (step 4.1): the software host's queue writer takes words of at most 64 bits (`waveflow/hw/mm_host.py:427`), as markov and mm_fir use; the engines are built at `word_bits` 64 for the system and its RTL gate, and the 128-bit option of the DSE is priced from engine-level builds (the engine's own harness runs 128 bits). A framework change to lift it was not made (not pre-approved) | doer, 2026-10-28; reported at the next stop |
| Decision | VS Code's git extension published `paper/ltbf_dse` to `origin` (`github.com/sdrangan/waveflow`) by itself (`git push -u` at 21:47:10, its `Git.log`), three seconds after the step 0.1 commit, and is expected to push later commits too. The user accepted the editor's pushes for this branch and said continue; the doer still never pushes, data stays gitignored, and the remote state is reported at milestones | user, 2026-10-28 |
| Decision | **The Ingest DMA's details** (step 4.3), within spec §5.4: (1) the O-RU applies the gain `2^-k` before its quantizer, so the DMA receives `Y = q_bc(2^-k y)` and moves words (a shift after a 16-bit quantizer would round twice and could not be bit-exact with the fixed chain); `IngestCfg` is the spec's `{enable, ring_depth, ring_base, chunk_words}` and `k` belongs to the front end's configuration; (2) every chunk of a symbol is written from its first antenna group on, so a symbol takes its `n_chunks` slots at once: `ring_depth >= n_chunks` (11 at `c` 8, `J` 9; 25 at `J` 4), and the spec's RTL-gate "ring depth 4" with `J` 4 holds only on a gate grid of 128 subcarriers (decided at 5.1); (3) the last chunk is zero-padded when `J·c` does not divide 792; (4) `Resp.seq` numbers the chunks that **arrived**, so a dropped symbol leaves a gap and the `k`-th record's slot is `k mod ring_depth`; (5) the credit is a register the view refreshes (in RTL a non-blocking read every cycle) | doer, 2026-10-29; reported at the next stop |
| Decision | **The scheduler's memory windows** (step 4.4): `plan_memory` places a graph's buffers so the plan holds under every order a policy may pick (two buffers share only when the dependencies order every use of one before every write of the other). Two knobs give it the order to reuse space: `cg_window` (default 2) -- CG job `q`'s `R₀ = D₀` copy waits for job `q - 2`'s `W`; `rt_window` (default 4, must exceed the last DMRS symbol) -- symbol `s`'s `Z` at a chunk position waits for the last use of symbol `s - 4`'s there. Both are DSE knobs; the ring is held longer when `rt_window` binds. On the small test system the plans take 1,792 of 4,480 words (long term) and 28,512 of 85,536 (real time) | doer, 2026-10-29; reported at the next stop |
| Decision | **The host reads the Ingest DMA's status when idle** (step 4.4): a drop after the last chunk seen shows no `seq` gap until a later record, maybe never; `Scheduler.on_status(chunks, overruns)` drops the next symbols' jobs once every chunk written has been seen | doer, 2026-10-29 |
| Decision | **The system's details** (step 4.5), within the spec: (1) **one DMRS stage a slot**, not one a chunk position: the estimator (spec §2) is full-band (one phase slope over the band, interpolation across it), so only a slot-wide stage equals the chain bit for bit; all of a symbol's chunks arrive together, so it costs no time; (2) **one vector-unit build** runs both programs (`programs.system_config`: the CG step's groups and queues, then the equalizer's at `Offsets`), since a build has one format a register; (3) **order-only edges** (`Job.after`) for `cg_window` and `rt_window`: a drop follows data edges only, and a dropped job completes as a no-op in dependency order, so its ring slots and order successors are released (without this one dropped DMRS symbol leaked the ring and stalled the slot); (4) **Shared Mem at bus address 0**, the devices from `0x8000_0000` (the spec's §5.2 map has them the other way; the engines address memory in words from 0); (5) a UE's `G'` columns share bus words with other UEs', so each CPU job rewrites `G'_stackᴴ` from the host's copy under a lock; (6) the release kernel and `ltbf_proj` are priced **provisionally** (`20 + 12·n_succ` cycles; 8 cycles a complex MAC + 30,000 for the Jacobi) until step 4.6 calibrates them | doer, 2026-10-29; reported at the next stop |
| Decision | **The A53 `Processor` is the host's** (`cpu.parent = host`): `HwSystem` refuses a root participant with no RTL lowering, and under XSI the host's C++ twin charges the same cycles with `compute(n)`, so the Processor is the host's resource, not a testbench participant. Example code only; the alternatives are a core semaphore around `compute(n)` (no `Processor`) or a framework change letting a `SwHost` own one | doer, 2026-10-29; **reported at the next stop** |
| Decision | **AC9's slot is paced at 500 µs a symbol**: at `word_bits` 64 one O-RU port carries a 64-antenna symbol in about 100 µs, not 8.9 µs (spike d), and each `Z` job is bus-bound at 28.9 µs (`G'_stack` is one row tile, `Mmax = S`, and is re-read for each of the chunk's 9 column tiles: about 6,900 words), 11 a symbol. At 8.9 µs the ring overflows and symbols drop; AC9 checks bit-exactness and RTL timing, and sustained throughput is the DSE's measure | doer, 2026-10-29 |
| Decision | **Accuracy-sweep trim** (step 3.2, within its "trimmed to ≤ 8 h"): four 32x32 jobs side by side took 948–1,034 s each (all CPU-bound at 100%, 4 GB resident each; the pilot's one 32x32 beside two smaller jobs took 301 s: memory-bandwidth contention of the integer projections that scale with N_rx), which put the full grid at 10–12 h. 32x32 keeps **30 evaluation drops (11–40)**, about 650 UEs a target; 8x8 and 16x16 keep all 90 (`EVAL_BY_ARRAY` in the runner, used by `run` and `merge`). The run restarted at 00:52 as two pools of 2 workers (32x32; 16x16 and 8x8), so at most two 32x32 jobs contend; the 7 finished jobs were kept (a job writes its file only when done). **Second trim (01:18):** under the run's own contention (the other pool, step 4.6's gem5 campaign) a 32x32 job took about 22 min and a 16x16 one 4 min, so the remainder was about 8.6 h of 4 workers against 7.1 h left: 32x32 keeps **20 drops (11–30)**, 16x16 **60 (11–70)**, 8x8 all 90 (about 5.8 h); both pools restarted, the 21 finished jobs kept. The budget is counted from the first launch (00:23): the sweep must end by 08:23 | doer, 2026-10-29; reported at the next stop |
| Decision | **Review of M2–M4** (step 4.7, a read-only `general-purpose` agent at `51b90030`; 16 findings, 6 major, no blocker). **Fixed (2026-10-29):** (1) `U_pad` was `S/r` in the system but `8⌈U/8⌉` in the graphs, so a sector with few UEs overran its `x̂` buffers: one `Dims.u_pad`, the equalizer's tile stride from `S`, and a system test on drop 1's sector 0 (5 UEs); (2) one job-id counter across both engines made each engine's Job FIFO words depend on the other's timing: ids per engine, and the static-order test now compares them; (4) the dispatch cap was jobs in flight = FIFO slots, so the vector unit (one slot) idled between jobs: slots + 1 (the running job), a queue write past a full FIFO waiting on the room interrupt as before; (6) the real-time graph's deadlines started at 0 and chunk roots counted as misses: the graph is built at the slot's start, chunks are arrivals; (8) **AC6's systolic test had 4 problems a job kind, not 20, and never checked `k_explicit`**: 20 seeds over three widths and the explicit `K` against `matmul` (AC6 was ticked early; now it holds); (5) AC7's re-run had no separate output: `subset` re-runs drops 11–12 over the grid into `paper_data/acc/rerun/` and compares job files and merged rows byte for byte; `merge` fails on a missing job. **Open:** (3) a vector-unit error leaves `VecWriter` waiting for stores that never come, so the host hangs instead of reporting: fixed in step 5.1 with the writer's HLS body (both twins); (13) the Ctrl registers are thinner than spec §5.4 (`enable` stored but not enforced, no `busy_cycles`, the vector unit's Ctrl reuses `SysCfg`/`SysStatus` and never publishes), and **AC9's Shared Mem is 132,973 words, over the spec's 1 MiB gate (131,072)**: step 5.2 sizes it (ring, plans) and checks it; (9) the system has no explicit form (no `HaHB` write): added with AC11's explicit runs; (14) `charge()` reads the Processor's last record, wrong if two charges end together (`n_cores` > 1): fixed before any multi-core run. **Accepted, recorded:** (7) the accuracy sweep runs regime 1 and one guard bit only (plan §2 lists both as axes): a trim for its budget, reported at the next stop; (10, 11, 12) the DMRS test's second port is on another CDM group, the DMRS op count is checked against the code (5r cmacs a subcarrier against the spec's 4r) and AC5 (b)'s test builds a whitened `z` directly: test gaps, revisited in the close-out review; (15) the scheduler bench's dispatch rule is the FIFO's room, the system's the cap; the no-deadlock test runs the dynamic policy; (16) `long_term_from_srs` fails on a drop with no connected UE (none in the 100) | doer, 2026-10-29; reported at the next stop |
| Decision | **The `release` cost model failed AC10 on its first form** (step 4.6): linear in `n_succ`, `n_ready`, fitted on 17 points, its held-out test median was **10.3%** (validation 10.8%; max 62.5% on the fit points at 1–4 successors), above the 10% bound. The corpus shows why: the ready decision's branch costs about 3.5 cycles a successor when outcomes are mixed and nothing when all or none become ready. My fit printed validation and test together, so the test score of that form was read; the **revised form** adds `mix = n_ready(n_succ − n_ready)/n_succ` (expected mispredictions of a random decision) and is scored once on a **fresh pre-registered test set** (`sweep.release_retest_rows`, 6 points, seed 202, committed before measuring). The first form's fit is kept locally (`model_release_v1.json`). `ltbf_proj`: test median 0.1% (max 0.2%) on its 12 held-out points; `ltbf_eig`'s model re-checked on 5 new inputs: median 0.37%, max 0.65% | doer, 2026-10-29 |
| Decision | **Framework fix (user-approved, 2026-10-29):** `waveflow/hw/arrayutils.py` `_array_utils_stem` spells '-' as 'm', so a fixed-point element with negative integer bits (`Ĥα'`/`G'` at 16 bits, −3 integer bits) names its array utilities `complex__fixed16_m3`, a C++ identifier (it was `complex__fixed16_-3`, which Vitis HLS rejects); other names unchanged; a unit test in `tests/hw/test_arrayutils.py` (19 passed; ruff counts unchanged). The user also accepted the decisions batched at this stop (64-bit bus, the host-owned Processor, AC9's 500 µs pacing, the sweep trims, `release` re-registered, the per-slot DMRS stage, the one vector-unit build, the review's dispositions) | user, 2026-10-29 |
| Finding (candidate, step 3.3) | From `accuracy_losses.csv` (340 jobs; medians over served UEs; loss against exact float LTBF on the same signals): **(F-a) width.** At `[-6, 3]` dB and `t` 3 (factored, `r` 1), W12 keeps 8x8 within 0.1 dB, 16x16 and 32x32 need W16; at `[-6, 14]` no width is within 0.1 dB at `t` 3 because float CG itself loses 1.2–3.4 dB there, and the quantization's own share at W16 is 0.08 / 0.06 / 0.09 dB (8x8 / 16x16 / 32x32). **(F-b) explicit vs factored `Q̂`** at W16, `t` 3: within 0.01 dB at 8x8 and 16x16; explicit worse by 0.19–0.29 dB at 32x32. **(F-c) more iterations hurt narrow widths**: at W ≤ 14 the loss grows past `t` 3 at 16x16 and 32x32, W12–14 collapsing to the SINR floor at 32x32 from `t` 3–5 on (fixed-point CG diverges); W16 drifts up 0.9 → 3.6 dB from `t` 5 to 10 at 32x32 `[-6, 14]`; W20 tracks float CG to `t` 10 | doer, 2026-10-29 |
| Open question | **Float CG's projection at `t` 2 is poor at 16x16 and 32x32**: median loss 56.7 dB at 32x32 `[-6, 3]` (5.6 dB at `[-6, 14]`, 4.4 at 16x16 `[-6, 14]`), while `t` 1 and 3 are near exact. Checked on drop 11, 32x32, `[-6, 3]`: (1) CG's `A` at `t` 2 is finite and nearer the exact solve than at `t` 1 (relative error 0.14 vs 0.37), so not a CG failure; (2) the paper's genie SINR on the same `G` is also bad (UE 1: −20.5 dB at `t` 2 vs −1.9 at `t` 1, −2.3 at `t` 3), so not the DMRS estimator: the `t` 2 iterate gives a poor projection for about half the UEs (12 of 22 at the SINR floor). Two hypotheses tested (Rule 4); not pursued further: the operating point is `t` 3; reported at the next stop | doer, 2026-10-29 |
| Decision (default, confirm at 1.4) | The Resp path: direct done streams from both wrappers and the Ingest DMA into a merge in front of the Resp FIFO's `QueueOut`, rather than writes routed over the crossbar | spec gate |
| Decision (default, confirm at 1.4) | `R̃ᵢ` from the long-term statistics (`GᵢGᵢᴴ` and `GᵢĤα`), not from DMRS residuals | spec gate |
| Decision | Plan review (2026-10-28, a read-only `general-purpose` agent): 16 findings, all acted on in this revision — the host pattern and AC9's comparison (§9 e), the systolic formats (thin Ctrl), the layout contract (`Ĥα` twice), the vector front end (one reader per stream, programs' size, scalars by `vdotr`), `R̃ᵢ`'s `GᵢGᵢᴴ` term and the genie SINR, the SRS estimator and `G_SRS`, AC3 against the applier's own response and the CP, on-the-fly rendering, the Ingest DMA, step 0.5's copy list, `.gitignore`, the baseline's order, the programs moved to 3.1, the Doppler figure, the existing `dispatch` family, the gate-floor bumps, the drop keys, AC13's measured time, AC9/AC11's configurations, the close-out `CLAUDE.md` proposal | plan author, 2026-10-28 |
| Assumption | Doppler inside a snapshot is zero: the drops store arrival directions at the BS but not departure directions at the UE, so per-path Doppler can't be computed. Over one 125 µs slot the fastest UEs (324 Hz) would rotate 0.25 rad, an EVM floor near −15 dB at the 14 dB target; the simulation omits it, the reference sees the same signals, so the loss metric is unaffected; stated as a limitation | user, 2026-10-28 (reuse chosen) |
| Assumption | Python stages (front end, estimators, measurement) take zero simulated time; their op counts are logged, not costed; the Ingest DMA's bus cost is | user, 2026-10-28 |
| Risk | The vector unit has no vector divide; the per-RE equalizer's rate is bounded by `wf.sdivz` (L divides per instruction at VL = 1) | spike 1.2 c; a vector divide is a framework change (stop) |
| Risk | Vector programs are straight-line and grow with the data; their Shared Mem traffic may matter | spike 1.2 c sizes it; the job-level simulator counts it |
| Risk | Per-iteration CG through Shared Mem moves X, R, D, U each iteration; with ingest, memory bandwidth may dominate | measured in the DSE (finding 4) |
| Risk | `SystolicEngineMem` at K = 1024 needs `Pmax` ≥ 1024/`Kmax` and a wide accumulator; `acc_format` caps the word width (about 26 bits); csynth time and memory unknown | spike 1.2 b |
| Risk | One systolic format triple for long-term and real-time jobs may cost accuracy on one of them | the accuracy DSE shows it; separate builds are a modeled nice-to-have |
| Risk | Full-band real time at 1024 antennas is far beyond one FPGA (about 9×10¹¹ complex MAC/s for the projection alone, and the ingest bandwidth) | reported as the sustained fraction (§2) |
| Risk | The host pattern (`SwHost` charging a `Processor`, with a C++ twin) is new in this repo | spike 1.2 e before the spec freezes |
| Risk | Single-path specular channels (the paper's D12): `Qⱼ` near rank one | stated limitation, as in `paper/cg_dse` |
| Open question | — | — |

## 15. Progress log

| Date | Step | What happened | Evidence | Deviation |
|---|---|---|---|---|
| 2026-10-28 | plan | Plan written (Draft) after the guided interview; readiness READY 15/15 | §8 | — |
| 2026-10-28 | plan | Independent plan review (16 findings); the user decided ingest (DMA), the systolic Ctrl (thin), the CP handling (timing advance, ISI kept) and the framework-edit pre-approvals; plan revised | §14 | — |
| 2026-10-28 | plan | The user approved the revised plan (including on-the-fly rendering); status Ready | — | — |
| 2026-10-28 | start | Execute mode: gate re-run READY (14 ready, 1 check: the untracked plan itself); the user said *Start* | gate output as §8 | — |
| 2026-10-28 | 0.1 | `paper/ltbf_dse` created from `main`; lessons file created; plan and lessons committed (`da3d0364`) | `git branch --show-current` = `paper/ltbf_dse`; `git rev-parse --short main` = `b0de5a26`; no remote | — |
| 2026-10-28 | 0.2 | Found `origin/paper/ltbf_dse` (VS Code `git push -u` at 21:47:10, the plan commit only). Stopped and asked; first told the user it was one-time, then corrected (the editor re-pushed later commits on earlier branches); the user accepted the editor's pushes and said continue | `git reflog show refs/remotes/origin/paper/ltbf_dse`; VS Code `Git.log` | Rule 9 stop |
| 2026-10-28 | 0.2 | `feat/sionna-prod` (`193dc4ef`) merged; only the two pre-approved conflicts: `CLAUDE.md` curated count → twenty-eight (`ls docs/examples/*/index.md \| wc -l` = 28); `tests/conftest.py` both histories kept + this merge's line, `WANT_XSI_GATES` = 246 (measured with `WAVEFLOW_NO_QUARANTINE=1 pytest -m xsi --collect-only -q`, the floor's own rule; without it 229 are collected and 17 quarantined count as present); gem5 12 and sionna 22 equal their floors. AC1: `pytest tests/rfenv -m "not sionna"` 78 passed; `pytest tests/build/test_xsi_session_gate.py` 20 passed; `pytest -m sionna -rs` 22 passed, 0 skipped, exit 0 | the commands | the floor is measured with quarantine off (the plan said plain `--collect-only`) |
| 2026-10-28 | 0.3 | Baseline fast suite (`pytest -m "not vitis and not xsi and not gem5 and not sionna" -q -rf`) after the merge: about 5,400 passed, **4 failed**, all environmental: `tests/build/test_xsi_workspace_copies.py::test_committed_copies_match_the_framework_source[vitis_fft]` (`examples/vitis_fft/xsi/run.bat` drifted from `waveflow/build/xsi/run.bat`), `tests/examples/test_vitis_fft.py::test_generate_writes_the_build_tree` (Vitis DSP FFT headers looked for under 2025.1), `tests/examples/test_markov_figures.py::test_every_manifest_figure_is_committed_and_recorded` (no `docs/examples/markov/images/sync_status.json`) and `::test_the_committed_figure_is_what_the_build_renders` (`chain_output.svg` stale). Ruff: `tests/conftest.py` 1 finding, `waveflow/cpu/calib/sweep.py` 0; ruff 0.16.9, black 26.5.1 | log in the session scratchpad (`baseline_fast.log`) | — |
| 2026-10-28 | 0.4 | `examples/ltbf_dse/.gitignore`; `examples/ltbf_cg/data/{denver,drops,a53_ltbf}` copied to `examples/ltbf_dse/data/` (12 MB, 2.6 MB, 96 MB); 3,221 files hashed into `data/MANIFEST.sha256` (local), whose SHA-256 is `ab0ca579e5c5b08e6fb4775e1c7fed38b9724c21e1638135f9316e474ed4f98b` | `git check-ignore -v examples/ltbf_dse/data/drops/drop_00001.npz` → `.gitignore:2:data/`; `git status --short -uall examples/ltbf_dse` → only `.gitignore` | — |
| 2026-10-28 | 0.5 | Copied from `paper/cg_dse` (`ba757884`): `examples/ltbf_dse/{scenario,ltbf_link,ltbf,ltbf_fixed,ltbf_eig}.py`, `cpu/ltbf_eig.c`, `cpu/sweep_plan.csv`; tests as `tests/examples/test_ltbf_dse_link.py`, `test_ltbf_dse_cpu.py`; `ltbf_cg` → `ltbf_dse` in paths and imports, a provenance paragraph in each docstring. `ltbf_eig_rows()` added to `waveflow/cpu/calib/sweep.py` (pre-approved; its rows reproduce the committed `sweep_plan.csv` byte for byte; ruff 0 findings, unchanged). Fast: 35 passed, 0 skipped; `pytest -m gem5 -rs tests/examples/test_ltbf_dse_cpu.py`: 2 passed, 0 skipped (gem5 v25.1). `WANT_GEM5_GATES` 12 → 14 (measured, quarantine off; pre-approved). Lint: ruff and black clean on the copies; `tests/conftest.py` keeps its 1 ruff finding | the commands | the gem5 floor raised here rather than in 4.6, when its new gates arrived |
| 2026-10-28 | 1.2 a | Spike a (`examples/ltbf_dse/spikes/spike_a_render.py`, output `data/spikes/spike_a.json`): the factored fast path (`A @ D`, rfenv's own kernel) equals `reference_apply` on 2x2 and `ChannelApplier` on 8x8 to 4.1e-16; one 32x32 sector slot (15,344 samples) renders in 0.13 s (three sectors 0.38 s) against about 48.5 s through `ChannelApplier`; 120 MB a sector slot in complex64. Delays over 100 drops x 2 snapshots (13,122 UE-sector links): after the per-UE timing advance none arrives early (the sectors share a site); 2.96% spread beyond the CP (serving links 2.99%), at most 4.22 us | the script's output | on-the-fly rendering uses the fast path |
| 2026-10-28 | 1.2 b | Spike b: csynth of `SystolicEngineMem` (Vitis HLS 2024.1) segfaults for every configuration tried, the unchanged centre included, at the core's free-running `STEP` pipeline (`hs_err_pid*.log`). Two hypotheses tested (size: refuted; pipeline style: confirmed): with `config_compile -pipeline_style flp` the centre builds in 49 s, 144 DSP, 32,937 LUT, 37,278 FF, 29 BRAM18, 3.352 ns (2024.1+flp; the engine alone at 2025.1: 154 / 31,718 / 36,354 / 32, 2.92 ns). Stopped (Rule 5 f); the user chose the flp workaround. Word-width cap from `acc_format`: W ≤ 26 at K = 1024, ≤ 27 at K = 240/256 (W = 16 needs a 43-bit accumulator at K = 1024) | `hw/build/spike_b/*/result.json`, `logs/flp.log` | stop for the user |
| 2026-10-28 | 1.2 c | Spike c (`spikes/spike_c_vecunit.py`, `data/spikes/spike_c.json`): the per-iteration CG step (state through queues, scalars recomputed with `vdotr`) and the r = 1 equalizer pass `asm.check` and run on the interpreter (CG step at 16 bits within 0.8% of numpy, R and D errors 16x smaller than at 12 bits: quantization, not the program; equalizer 7e-4). Priced with the calibrated `VecTiming` (extrapolated): CG step k = 1024, n = 8, L >= 8: 8,679 cycles (34.7 us); moves 7 k n elements (14,336 128-bit words), so memory-bound at a word a cycle; `Kmax * ceil(Nmax / L) <= 4096` caps a job at 64 columns at N_rx = 1024. Equalizer about 29 cycles a subcarrier at L = 16 (0.41 RE/cycle). Pass rules: one write a group a pass; broadcast is lane 0 to all lanes; `.vs` scalars are real, so per-subcarrier complex weights cannot span symbols | the script's output | the equalizer's layout and divide placement go to the spec gate |
| 2026-10-28 | 1.2 d | Spike d (`spikes/spike_d_memory.py`): on-chip 7.56 MB. Long-term factored set fits at every size (2.95 MB at 1024); explicit at 1024 needs 6.95 MB (DDR model when real time runs beside it). Full-band ingest needs 5.7 / 22.7 / 91 128-bit ports at 64 / 256 / 1024 antennas: one port sustains about 18 / 4.4 / 1.1% of the band | the script's output | — |
| 2026-10-28 | 1.2 e | Spike e: (1) pysim (`spikes/spike_e_host.py`): a `SwHost`'s threads charge a 1-core A53 `Processor`; each call padded to whole host cycles (268 / 143 / 268 as expected), threads serialize, an `SwEvent` wakes at the set instant; a queued call can start up to one host cycle earlier than behind the C++ twin's core semaphore (exact with one scheduler thread). (2) RTL: markov's C++-host system gates fail here (`ap_int.h` not found: `find_vitis_include_dir` misses 2024.1's `Vitis_HLS/2024.1/include`); with that directory supplied in memory, markov's whole system flow passes (system RTL, XSI with the C++ host, `compare`). The build appended one record to each of three tracked markov calibration files: restored with `git checkout`, the five `*_proj` csynth directories removed. Stopped (Rule 5 d); the user chose the framework fix | logs in the session scratchpad | stop for the user |
| 2026-10-28 | 1.2 e | Framework fix (user-approved): `find_vitis_include_dir` falls back to `Vitis_HLS/<version>/include`; 3 new unit tests, `tests/toolchain/test_toolchain.py` 17 passed; the real lookup now gives `/tools/Xilinx/Vitis_HLS/2024.1/include`; ruff counts unchanged (`toolchain.py` 80, the test file 1); `tests/build/test_sw_schema.py` passes. Unpatched, markov's `-m xsi` gates on this machine: 6 of 8 pass (bit-exact, never polls, host traces match pysim, three sub-tops); `test_markov_rtl_cycles` and `test_markov_pysim_tracks_rtl` fail on the cycle count, RTL 1,973 (Vitis 2024.1) against the 1,870 recorded with 2025.1 (pysim 1,856, 5.9% off): a tool-version difference, not the fix. markov's calibration records and `*_proj` restored again | the commands | — |
| 2026-10-28 | 1.1 | Spec drafted: `plans/ltbf_dse/ltbf_dse_spec.md` (signals, estimators and op counts, the algorithm with whitening folded into `G'`, formats, the system: diagram with RespHub and Ingest DMA, address map, the **layout contract** (`Mmax = Kmax = 8`, `Nmax = c`; every exchanged matrix one tile wide in its role), descriptors and registers, job graphs, scheduler, host); seven open choices E, W, F, R, C, G, P for the gate | the file | spike e's RTL evidence is markov's flow, not a toy of this system (the system gate is step 5.2) |
| 2026-10-28 | 1.3 | Spec reviewer (read-only `general-purpose` agent): 3 blockers (B1 the `Y` chunk's B layout from tile-contiguous writes; B2 the vector unit's single program reader + splitter and variable-length descriptors deadlock; B3 198 comb subcarriers make the 8 SRS shifts fractional), 6 major (formats and the lane limit; `W = ĤαᴴX` replaces three products; long-term jobs unsplit; Python results' path to Shared Mem; uncalibrated CPU work; the measured SINR), 12 minor. Verified before acting: B1 in `systolic_engine_mem.py:197–208`, M1 in `formats.py:54–66`, B3 by a new spike f (`spikes/spike_f_srs.py`: 198 leaks -10 to -20 dB on a flat channel; 192 exact on whole taps, -20 dB at half a tap with lead 4 / taper 4, `G_SRS` 16.14 dB); the gate-E equalizer added to spike c (legal, 6e-4, 0.42–1.55 UE·RE a cycle). Spec v2 acts on every finding (its §7) | the spec's §7 | — |
| 2026-10-28 | 1.4 | 👁 The user approved spec v2 with every recommendation (gate E: `w` from the Python estimator; W Cholesky into `G'`; F 16-bit lanes, 32 an axis; Q the paper's `Q̂`; P 64 RBs / comb 4 / 8 shifts / lead and taper 4; D DMRS +4.77 dB; C knobs, RTL gate `c` 8, `Mmax` 16, `J` 4, ring 4; G `G'` on the CPU). Frozen: `plans/ltbf_dse/ltbf_dse_spec.md` SHA-256 `40979b9653f5f98bfaafc0f44838824eca61ea41237b47a868d64d3e49545d88` (AC2) | `sha256sum` | — |
| 2026-10-28 | 2.1 (rest), 2.2 | `signals.py` gains the spec's waveforms (SRS on 64 RBs, comb 4, 8 shifts, ZC 191→192, amplitude 2; PUSCH: QPSK data on 12 symbols, type-2 double-symbol DMRS on 2-3, boosted 4.77 dB, ports by rank in the serving sector, a sequence per sector) and `received()` (per sector, rendered + noise + front end). `chest.py`: the SRS estimator (each port's filter as a `192 x 8` linear map, one counted product a comb), `long_term_from_srs` (into `ltbf_link.LongTerm`, so the copied CG and projection code is reused), the DMRS estimator and weights; `opcount.OpLog.note`. `tests/examples/test_ltbf_dse_chest.py` 4 passed (AC4): whole-tap leakage < -30 dB on 30 flat UEs; error variance at G_SRS 16.14 dB ± 1; DMRS exact on a flat channel, on a delayed one exactly `cos(δ/2)` (FD-OCC despreading averages two adjacent subcarriers: 0.055 dB at 300 ns); op counts equal spec §2 for three SRS and two DMRS shapes | the tests | AC4 (b)'s delayed case asserted at its predicted `cos(δ/2)`, not 1e-9: inherent to despreading before delay removal |
| 2026-10-28 | 2.3 | `ltbf_chain.py`: prepare (render and estimate once), project (`G`, Cholesky-whitened `G'`), real time (projection, DMRS weights, equalizer), measure (MMSE and scale-invariant SINR, EVM, BER), the genie SINR. `tests/examples/test_ltbf_dse_float.py` 7 passed: AC5 (a) CG without the paper's stop reaches 1e-12 residual and the direct solve to 1e-8 on estimated statistics (both forms and regimes); AC5 (b) measured SINR = `1/mean(1/(1+γ)) - 1` within 0.1 dB; whitening identity; determinism. Drop 1, 8x8, [-6, 14]: exact LTBF measured 2.89 dB against genie 3.16 dB; t = 3 at 2.22 dB | the tests | — |
| 2026-10-28 | 2.4 | Paper check (`ltbf_paper_check.py`, 10 drops x 3 arrays, target [-6, 3], r = 1, 216 served UEs an array; `results/paper_check.csv` local). Median instantaneous MMSE minus exact LTBF, genie SINR on this chain's `G`: **1.02 / 2.37 / 5.17 dB** at 8x8 / 16x16 / 32x32 (`paper/cg_dse` with the paper's SRS model at 10 dB: 2.38 / 6.27 / 10.71; the paper 2.25 / 6.30 / 10.58): the same growth with the array, smaller because the rendered and estimated SRS reaches G_SRS about 16 dB. Against the realizable receiver's measured SINR: 1.29 / 2.68 / 5.56 dB (measured minus genie -0.31 / -0.31 / -0.40 dB). CG t = 3 loss: 0.016 / 0.089 / -0.006 dB (genie), 0.028 / 0.090 / -0.005 dB (measured) | the log | AC5 (c) recorded, not a gate |
| 2026-10-28 | 3.1 | `hw/programs.py` (the CG step: per iteration through queues, `S = q_S(U + 2^-2s D)` by `wf.vmacc.vs` with a scalar 1 and the result shift, the last iteration's `X_b` store; the equalizer on Z tiles) and `ltbf_fixed_chain.py` (formats from profiled ranges; the systolic jobs by `ltbf_fixed.mm`; the CG step by `S` then `linalg.cg.vec_step`; the CPU job from `W` and `X`; the equalizer on stored integers). Two design problems found and fixed: one shared `bc` collapsed values (spec amendment 1, approved: `X` its own format and `X_b`, gains on `Y` and `G'`), and `VecUnitConfig` needs integer bits >= 1 (an RHS gain `2^q` lifts `D` into `[0.25, 0.5)`; only `a` keeps negative integer bits). AC6: `tests/examples/test_ltbf_dse_fixed.py` 44 passed (20 systolic jobs = `linalg.matmul`, 20 CG steps and 20 equalizer tiles = `vecunit.model.run_job` of the real programs, zero divisor included). Drop 1, 8x8, t = 3 vs float CG: median loss 2.18 / 0.73 / 0.013 / 0.002 dB at W = 10 / 12 / 14 / 16; explicit W16 0.001 dB | the tests; the smoke run | spec amendment 1 |
| 2026-10-28 | 4.1 (pysim) | `hw/records.py` (`Resp`, `SysCfg`, `SysStatus`), `hw/sys_wrap.py` (`SysWrap`: `SystolicEngineMem` unchanged, `SysTap` turning `EngineDone` into a `Resp` and the Ctrl status, `SysCtrl`; views `qjob` `QueueIn`, `ctrl` `RegBank`), `hw/resp_hub.py` (`RespHub`, round-robin into a `QueueOut`). `tests/examples/test_ltbf_dse_hw.py::test_systolic_wrapper_runs_jobs_from_its_job_fifo` passes: a `SwHost` writes the engine's test jobs into `qjob` over a 64-bit crossbar, sleeps on the `qresp` IRQ, gets one `Resp` (src 0, its tx_id, OK) a job, and every `C` in Shared Mem equals `linalg.matmul` | the test | the bus is 64 bits (§14) |
| 2026-10-29 | 4.2 (pysim, CG step) | `hw/vec_wrap.py`: `VecWrap` around the unchanged `VecUnit`: `VecDecoder` (a fixed-size affine `VecMemJob`: program addresses and lengths, up to 4 load and 4 store streams walked `count` rounds), three program readers and a data reader (`MemRStream` in-band, one a stream), `VecWriter` (each `VecResp` + data to an in-band `MemWStream` write; the job's last store forwards the `Resp`, after the `VecStatus`, emitted only once the data is in memory), `VecCtrl`; views `qjob`, `ctrl`. `test_vector_unit_wrapper_runs_a_cg_step_from_shared_mem` passes: the programs and X, R, D, U in Shared Mem, one `VecMemJob`, one `Resp` (src 1, tx 7, OK), and X, R, D, X_b in memory equal `linalg.cg.vec_step`'s step bit for bit (W14, s = 2, the last-iteration variant). Program addresses ride in the descriptor (the spec's program table, without a fourth reader); stored programs carry tx_id 0 | the test | descriptor carries the program table |
| 2026-10-29 | 4.3 | `hw/ingest.py`: `IngestDma` (corner-turn buffer of `Kmax` antennas, panels `(p, j)` of every chunk at `slot + (p·J + j)·wb` through an in-band `MemWStream`, one `Resp` src 2 a chunk forwarded after its last panel; the `CreditIn` view's cumulative count; a symbol without `n_chunks` free slots dropped and counted) and `IngestDev`; `IngestCfg`, `IngestStatus` in `hw/records.py`. `test_ingest_dma_writes_chunks_in_the_systolic_b_layout` (16 antennas, `J` 4: 25 chunks, the last padded; 3 symbols; every slot = `block_major` of its chunk, seq 0..74, status 75 / 0) and `test_ingest_dma_drops_a_symbol_without_credit` (a ring of 11 never freed: symbols 1 and 2 dropped, status 11 / 2; 11 credits back, symbol 3 into the same slots as seq 33..43, status 22 / 2) pass; the file's 4 tests in 0.6 s. Found on the way: a DMA that drains the credit stream only at a symbol's start reads the oldest values the stream buffered and drops symbols it had room for. On one 64-bit O-RU link the three 16-antenna symbols end at 128 µs of simulated time against a symbol every 8.9 µs (spike d's ingest bandwidth) | the tests | the details in §14 |
| 2026-10-29 | 3.2 | Found that the sweep's workers ran 8 OpenBLAS threads each (numpy imported before `_threads1()`, workers forked; `threadpoolctl`): every command now runs in spawned single-threaded workers (the mimo_cg pattern), committed `6a58de9f`. Profiles re-run single-threaded: within 4e-10 relative of the threaded ones; the three pilot jobs byte for byte the same. Pilot, three jobs side by side: 45.4 / 122.6 / 301.6 s at 8x8 / 16x16 / 32x32. The grid (90 drops x 2 targets x 3 arrays = 540 jobs) is about 84,500 CPU-s, 5.9 h on 4 workers: within 8 h, **no trim**. Launched detached at 00:23 (`setsid nohup nice -n 10 ... run --workers 4`, `logs/acc_run.log`) | the logs | — |
| 2026-10-29 | 4.4 | `hw/graphs.py` (`Dims`; `long_term`: SRS, explicit `K`, per CG job `t` x (`T`, `U` or `KD`) + the CG step, `W`, a CPU job a UE, products split by `B_LT`, `cg_window`; `real_time`: chunk roots in arrival order, `Z`, the DMRS stage, the equalizer and the measurement, `rt_window`) and `hw/sched.py` (`Scheduler`: dynamic two-class EDF on `sched_ops` lists and static per-engine order; flow control `in flight <= qresp - ring`; Ctrl writes on a configuration change; in-order ring credit; drops from `seq` gaps and from the DMA's status; fatal errors; the `release` twin; every decision in a priced trace; `plan_memory`). `tests/examples/test_ltbf_dse_sched.py`: 16 passed on an event bench -- dependencies (both policies, both forms), the spec's graph shapes, no deadlock at Job FIFOs of 1 / a ring of one symbol / `qresp` = ring + 1, the Resp FIFO's peak within its depth behind a 2 µs-a-record host (it reaches 15 of 15), the plans hold under the run's real lifetimes (583 and 6,718 overlapping pairs checked), a constructed case (2 overruns, 1 miss, credit 2), an engine error, in-order credit, Ctrl writes, the static order unchanged under other timing, the trace's counts. A probe first found two gaps the tests now pin: trailing drops never seen (the status read) and no real-time reuse (the window) | the tests; the probe | §14 (windows, status read) |
| 2026-10-29 | 4.2 (equalizer) | `test_vector_unit_wrapper_equalizes_a_chunk_by_affine_walks` passes: `eq_programs` (`r` = 2, W14) on a chunk of 3 tiles as one `VecMemJob` of 3 rounds, `Z` and `w` in the spec's C-tile layout (load stream `r` at `base + r·wb`, stride the tile), `x̂` a tile a store; every tile equals `equalize_fixed` bit for bit; one `Resp` (src 1, tx 9, OK). With the CG step, both of `hw/programs.py`'s programs run through the wrapper: step 4.2 done | the test | — |
| 2026-10-29 | 4.5 | `hw/system.py`: `inputs` (prepare, profile on the drop, formats, the stored `Ĥα'`, `B'`, `Y`, and `golden`: the fixed-point chain on the sector), `LtbfHost` (`SwHost` + the A53 `Processor`; a dispatcher thread running the scheduler, a `qresp` reader, CPU workers, a status ticker; every scheduler decision charged on its calibrated family; each host job's writes and cycles recorded), `LtbfSystem` (the memory map: programs, `G'_stackᴴ`, the ring, the two plans; `EngineJobDesc` and `VecMemJob` from the graphs and plans), `OruSource`, `build` (9 masters on one 64-bit crossbar), `check`, `write_replay`. Shared with the chain: `ltbf_fixed_chain.project_ue` (the CPU job, factored out; one pilot job re-run byte for byte the same), `programs.system_config`. `test_system_equals_the_fixed_chain[static, dynamic]` pass (15.8 / 8.3 s): drop 1, sector 1 (10 own UEs, 22 connected), N_rx 64, `t` 3, `r` 1, `c` 8, `Mmax` 16, `J` 9, ring 2 symbols, the slot at 500 µs a symbol: 694 jobs, none dropped, `G'`, the DMRS `Z`, `w` and every `x̂` tile equal the chain's bit for bit. Simulated: the long-term update ends at 3.0 ms; the slot's `Z` and equalizer jobs are bus-bound at 28.9 µs each. Found on the way: drops cascading through window edges leaked the ring (fixed: order-only edges, no-op drops); `HwSystem` refuses the `Processor` as a participant (§14) | the tests | §14 (system details, the Processor, AC9's pacing) |
| 2026-10-29 | 4.6 (part), 4.7 | 4.6: `cpu/ltbf_proj.c` (the per-UE job: `M` from `W`, the Jacobi, `G`, the cross terms, `R̃`, Cholesky, `G'`) and `cpu/release.c` with bit-exact twins (5 and 4 host-build points equal), `a53cal.py` (ltbf_eig's flow made reusable), rows in `sweep.py` (`ltbf_proj_rows` 52, `release_rows` 27, `ltbf_eig_recheck_rows` 5; additions only, the existing rows unchanged) and the plan files committed before any measurement (`51b90030`); `WANT_GEM5_GATES` 14 → 18 (measured, quarantine off); the 6 gem5 gates of `test_ltbf_dse_cpu.py` pass, 0 skipped (gem5 v25.1); the campaign runs detached; the system prices both kernels from their local fits when present. 4.7: the reviewer's 16 findings and dispositions in §14; findings 1, 2, 4, 5, 6, 8 fixed with tests (`test_ltbf_dse_{sched,hw,fixed}.py`: 16 + 8 + 66 passed) | the tests | AC6's systolic coverage corrected (§14) |
| 2026-10-29 | 4.6 | The gem5 campaign (v25.1, 2 workers, 12.2 min; 84 points, every twin matched): `ltbf_proj` fit 32 / validation 8 / test 12 points, held-out test median **0.1%** (max 0.2%); `ltbf_eig`'s model (from `paper/cg_dse`) on 5 new held-out inputs: median **0.37%** (max 0.65%); `release`'s first form failed (test median 10.3%, §14), the revised form (with `mix`) on 6 freshly registered test points: median **1.8%** (max 13.5%; validation 6.5%). Models local (`data/a53_ltbf/model_{ltbf_proj,release}.json`); `test_the_fitted_models_meet_ac10` checks them. **AC10 met** (with step 4.4's unit tests) | the campaign log; the fits | `release` re-registered (§14) |
| 2026-10-29 | 3.2 | The accuracy sweep's grid is complete: 340 jobs (8x8 180, 16x16 120, 32x32 40), the last written at 06:05, 5 h 42 min after the first launch (00:23): within the 8 h budget | `ls paper_data/acc/jobs` | the trims in §14 |
| 2026-10-29 | 5.1 (stop) | Stopped (Rule 5 d): the systolic top's csynth failed on `complex__fixed16_-3_array_utils` (a negative-integer-bit format's name); the user approved the one-line framework fix and accepted the batched decisions (§14). Meanwhile `ltbf_hub`, `ltbf_hub1` and `ltbf_ingest` synthesize (Vitis HLS 2024.1, 34–48 s each); the vector top's csynth hung in hardware synthesis for 6.5 h (stopped; investigated next) | the logs | — |
| 2026-10-29 | 3.3 | `ltbf_accuracy_figures.py`: per target, loss vs `t` by width, explicit vs factored and rank 1 vs 2 against width (6 SVGs in `paper_data/figures/`, local; byte for byte the same on a re-run); findings F-a–F-c and the `t` 2 open question in §14. Colours: the dataviz reference palette's pre-validated values (its validator needs Node, not installed here) | the figures | — |
| 2026-10-29 | 5.1 (stop 2) | Stopped (Rule 4 / Rule 5 d, f). (1) The Ingest gate: RTL bus and slot contents equal pysim's; every `Resp` the C++ host records has bits 47:32 = 0xffff although the kernel, the hub and the queue view's bus RDATA carry the right `seq` (VCD); the queue view's read data shows X in exactly those bits at one point. A framework question: the user chose **investigate only, then ask**. (2) The vector unit: the equalizer's unit alone synthesizes in 26.5 min, the CG step's alone exceeded 45 min, the combined top ran 6.5 h: the user chose **measure longer** (4 h cap each, the stalling phase profiled), the other gates meanwhile. (3) The systolic gate hangs (the host waits for its first `Resp`, 1,000,000-cycle bound); the framework's own flp engine harness passes RTL = pysim (1.3 min), so the cause is this example's (investigating). Fixes found on the way: scenario bursts promoted to float64 (now `uint64`), plain reads over 256 beats, the engine's done link framing, `xsi_work/` ignored | the logs, the VCD | stops for the user |
| 2026-10-29 | 5.1 | **The Ingest DMA's RTL gate passes** (RTL = pysim on every endpoint; Vitis HLS / Vivado 2024.1; `ltbf_ingest` 3.352 ns, 20 BRAM18, 4 DSP, 3,267 FF, 3,567 LUT). The `seq` 0xffff the investigation (user: investigate only) chased was not the framework's queue: a VCD showed the kernel, hub and bus correct, and a printout in the C++ twin showed every record one symbol late -- the DMA's state kept in statics across firings was read one firing stale in RTL (C simulation right); a never-returning body with loop-carried state fixed it (lessons). **The systolic gate hangs because of the engine, not the wrapper**: the framework's own `SystolicEngineMem` harness built with flp (Vitis 2024.1) passes RTL = pysim at `Pmax` 4 and 8 (Mmax 8 or 16) and fails at `Pmax` 11, 16 and 22 the same way (1 of 3 jobs done, then no more reads); AC9's `U = Ĥα'T` needs `K` = 176 (22 panels at `Kmax` 8). The vector unit's CG-step-alone and combined builds are in HLS hardware synthesis (4 h cap, from 09:40) | the harness runs; the VCDs | stop for the user (the engine) |
| 2026-10-29 | 3.2 | **AC7 holds**: `subset` re-ran drops 11–12 over the whole grid into `paper_data/acc/rerun/`: 12/12 job files identical to the sweep's and the merged rows identical (`logs/acc_merge_subset.log`); the figures were byte-stable already (3.3) | the subset log | — |
| 2026-10-29 | 5.1 (stop 3) | **The systolic engine's cause (user: investigate the engine)**: a deadlock in `systolic_engine_feed_task`, not flp. Per tile the feeder issues all `2P` `read_request`s (REQ) before it reads a word (WORDS). Vitis 2024.1's `m_axi` read adapter (defaults `num_read_outstanding` 16 x `max_read_burst_length` 16 = a 256-word read buffer, a request queue `USER_MAXREQS` 7) takes a request only while it has room, and its buffer drains only as WORDS reads. The ltbf_sys VCD: job 0 (`T`, P 8, 16 requests) is read whole (8,448 words); job 1 (`U`, P 22) has 17 of its 44 requests taken (5 = A0 B0 A1 B1 A2 = 256 words in the buffer, 12 queued), the feeder waits on ARREADY for the 18th and never reads, so nothing drains. This matches every harness run: 2P requests of 32 words fit at P <= 8 (pass at 4, 8) and not at 11, 16, 22 (fail). C simulation's `burst_maxi` queue is unbounded and pysim reads panel by panel, so only RTL sees it; every recorded engine number is at `Pmax` 4. Proposed fix (framework, awaiting the user): request at most 6 panels ahead, then one more at each panel's last word inside WORDS | the VCD, the harness runs | stop for the user |
| 2026-10-28 | 2.1 (part, ahead of the gate) | While the spec reviewer ran (Rule 5): the spec-independent half of step 2.1, `examples/ltbf_dse/signals.py` (timing advance, sector paths with power control, the factored rendering, rfenv noise, unitary OFDM, the ideal frequency response) and `frontend.py` (CP removal, FFT). `tests/examples/test_ltbf_dse_signals.py`: 4 passed -- AC3 (a) render vs `reference_apply` on 2x2 ≤ 1e-9; AC3 (b) on a **periodic** symbol (every delay a cyclic shift) the grid equals `sum_u H_u X_u` ≤ 1e-3; AC3 (c) bytes identical, noise unit a RE; the OFDM round trip. Measured on 3 drops x 3 sectors of a real slot (noise off, 4 antennas): serving links within the CP, rendering error median -311 dB, worst -54.7 dB (fractional delays: the 64-tap interpolator's tails across symbol boundaries); the 2 beyond the CP -23 to -25 dB (real ISI, kept) | the test; the measurement script in the log | AC3 (b) tested on a periodic symbol: on a full slot the worst in-CP link's amplitude error is 1.8e-3, just above the literal 1e-3, and it is the interpolator's boundary smearing, not the conventions under test |

## 16. Completion report

*Filled in when the work ends.*

- **Changed:**
- **Tested (commands and results):**
- **Acceptance criteria:** AC0 … AC15 ✅/❌/⚠️ with evidence
- **Decisions made:**
- **Assumptions and not verified:**
- **Remaining risks and issues:**
- **Next steps:**
- **Reusable artifacts saved / tools that would have helped:**

## Checklist coverage

| Group | Addressed (where) | Not applicable (why) |
|---|---|---|
| T: task definition, planning | §1–§4 (goal, claim, requirements, AC0–AC15); §10 steps with exit conditions; Rules 2–5 | — |
| C: context and environment | §5 sources with access; §6 environment; §7 existing system, reuse, conventions | C4 repo overview: `CLAUDE.md` and `docs/guide` are current |
| V: verification | §4 Verify lines; §11; Rule 4 (root cause, 2 hypotheses) | — |
| D: documentation and continuity | lessons file (Rule 3); the spec; §16; `CLAUDE.md` count in 0.2; step 7.2's proposal of `CLAUDE.md` / guide additions | — |
| R: version control and safety | §12; Rules 8, 9, 12; branch and per-step commits | irreversible actions: none planned |
| S: security and permissions | §12 permissions | secrets: none needed |
| A: authoritative sources | §5 with versions; Rule 2 | — |
| P: plugins, MCP, tools | §8 capabilities; §9 search before building | `.mcp.json`: the waveflow MCP is already configured |
| M: multi-agent | §11 reviews; §13 | — |
