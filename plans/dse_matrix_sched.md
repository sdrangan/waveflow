# Plan: pre-silicon design of a matrix scheduler -- which whitening matrices to compute before a deadline

**Status: DRAFT for discussion, written 2026-11-22** from a long design conversation with the user (cloud
session `session_01HnWt85iSFAfaupHA1sSwTL`, branch `ccr-62d70dbe-679k63`). Nothing here is built. It
**replaces the next step of `plans/dse_cg.md`** (paused; why below) and gives `plans/dse_arch.md` its first
concrete problem. Section 9 lists the decisions still open; Section 11 records the conclusions of the same
conversation that are not this study but that a later agent will need.

**For the agent picking this up:** read this whole file first, then the files Section 10 names. The user's
answers are quoted as decisions (D1...); do not re-open them without asking. Where this plan says *proposed*,
it is a recommendation awaiting the user.

## 1. Why this study, and why not more parametric DSE

The question behind every DSE study here: **does Waveflow's pysim (bit-exact, calibrated cycles and
resources) make design exploration better than the standard tools -- an untimed bit-exact model, HLS
estimates, synthesis and RTL simulation?**

What the earlier studies found:

- **`plans/dse_cg.md` (CG on the LTBF system, parametric).** G1 measured 768 RTL runs (174 tool-hours);
  pysim scored within its targets (cycles 1.44% mean error, LUT 4.1%, BRAM exact) and picked all three
  optima blind. But in the agent pilots **every agent in both arms submitted the measured optimum**; on
  tight-deadline objectives the simulator arm was only **1.1-1.8x** faster to target, and calibration
  amortized over 18 runs paid for itself on 2 of 6 objectives. The baseline agents learned the space from
  a few RTL runs. **Paused by the user (2026-11-22):** the design used general units on an interconnect,
  which gave poor throughput, so its optimum is the optimum of a weak architecture. The design is to be
  reworked before any further study on it.
- **`plans/toy_shared_memory.md` (a toy with long simulations).** The speed-up was small at first because
  of a poor RfDC model in pysim; improved since, but the toy needs richer blocks (an FFT) to matter.
- **The general lesson (user and agent agree):** when every hardware knob is monotone -- more resources,
  more performance -- an agent maxes the knobs to the resource limit and confirms with one or two RTL runs.
  Pysim has nothing to add. A fully parametric version of any problem whose knobs are monotone repeats
  dse_cg's result.

**What does need many timed simulations** (worked out in the conversation):

1. **Timing that changes the answer** -- work that misses a deadline is cut short or dropped, so accuracy
   itself depends on cycles, and the fast untimed model cannot score a design.
2. **A rare-event objective** (a tail or a miss rate) -- each evaluation needs thousands of windows.
3. **Service time that varies with data and with contention** -- data-dependent iteration counts are easy
   to hand-model from the untimed model; contention on a shared memory, an arbiter or credits is not.
4. **Choices that are not monotone.**

**The user's key observation (2026-11-22):** even with the hardware fixed and its knobs maxed, the
**run-time policy that decides what the hardware computes** is not monotone, and evaluating a policy
needs a timed simulation of the hardware. Designing that policy before silicon is the study.

## 2. The premise: pre-silicon

**D1 (user).** The study models **pre-silicon** design: the scheduler is developed while the hardware is
not yet built. In the user's words, this is 99% of the design studies at Qualcomm. Hardware in the loop
is impossible for an ASIC, and for an FPGA costs a bitstream per hardware variant. So the baseline's
timed options are RTL simulation and a timing model it writes itself -- not a board. Say this in the
paper's threats to validity (FPGA reviewers will ask "why not run it on the board").

## 3. The problem: a matrix scheduler

Built on the LTBF receiver (`plans/ltbf_dse/ltbf_dse.md` Section 1.1; `examples/ltbf_cg_mem_dse`): CG
solves against the interference-plus-noise covariance, `Q̂ = I + ĤαĤαᴴ`, `Q̂Aᵢ = Ĥᵢ`. In this variant
the whitening matrix (the user calls it R̂) depends on **which users are active in a resource block (RB)**,
so it is computed per user set.

The pipeline, per window (**D2, user 2026-11-22**):

1. **User scheduler -- fixed, given.** Long-term beamforming, so before the window it selects the user
   sets likely to be used in the window. Precomputed before the instantaneous scheduling decisions.
2. **Matrix scheduler -- the design under study.** From the candidate sets it chooses which whitening
   matrices to compute -- for which sets or supersets, with how many iterations, in what order -- and
   hands them to the engine as jobs.
3. **Engine -- fixed hardware.** Computes the jobs in order. **A deadline hits.** What finished is
   available; a job not finished is **discarded** (D5).
4. **Instantaneous scheduling.** Users are assigned to RBs. Each RB uses the whitening matrix computed
   for the **closest superset** of its actual user set (D4). With none available, **no whitening**: its
   interference is treated as noise (D3).

The user's answers:

| | decision (user, 2026-11-22) |
|---|---|
| D3 | No matrix covers an RB's set: **no whitening**, the interference counts as noise. |
| D4 | "Closest superset" is a **fixed rule** of the evaluation, for now. *Proposed:* the smallest computed superset (fewest extra users); ties to the one computed with more iterations. |
| D5 | **Partial solves are discarded**, for now (can change). But **the iteration count is a parameter of each job**, chosen by the scheduler -- the user's "number of items as one of the parameters given to the engine". *To confirm with the user that this reading is right.* |
| D6 | The matrix scheduler sees **each user's SNR and the channel correlations** (so it can tell when two users are spatially close). |
| D7 | The timeline: **5 ms**, for now. |
| D8 | The matrix scheduler runs **on the A53** (`waveflow.cpu`, the calibrated `a53_hpi_1200mhz_gem5v25_1_vitis2025_1` platform): its own run time comes out of the budget. |

**The trade the scheduler makes:** coverage (some computed superset contains the RB's set -- an uncovered
RB pays the full interference) against tightness (extra users in the superset cost degrees of freedom)
against cost and order (each job takes time and the deadline cuts the list's tail; a discarded job is pure
waste). Natural shapes: broad "insurance" supersets early, tighter ones after; merging candidate sets that
differ by users who are spatially far apart (D6 makes that visible, and such an extra user costs little);
iterations per job traded against number of jobs; streaming the first jobs to the engine while the A53 is
still planning (D8).

**Where timing changes the answer:** step 3. How far down the list the engine gets depends on each solve's
time, which depends on its set's size, its iterations, the channel's conditioning (if the solve may stop at
a tolerance) and contention inside the engine. So the SINR is a function of the timing, and only a timed
run scores a schedule. The untimed model can only bracket it: every candidate exact (upper bound), nothing
computed (lower bound).

**Rough load (an estimate, to redo once the sizes are fixed):** a CG job on the vector unit took about
3,400 cycles at dse_cg's sizes (`plans/dse_cg.md` open question 3: 3,447 at 2025.1), about 14 µs at 250 MHz,
so roughly 350 jobs a unit in 5 ms -- the same order as a window's candidate sets. The budget should bind.

## 4. Two studies

- **S1: the policy only.** Fixed hardware, pre-built and verified. The agent writes the matrix scheduler.
  Scored on held-out drops and traffic.
- **S2: co-design.** The agent chooses the hardware point *and* its policy. The objective is **the fewest
  resources that meet the SINR target under the best policy** -- a smarter policy may let two CG units meet
  it where a naive one needs four. That is a nested search: the inner loop (a policy per hardware point)
  needs timed simulation, and the outer loop multiplies it. Even on an FPGA a board cannot serve the outer
  loop (a bitstream per point).

**Score:** the mean and the 99th-percentile SINR loss against a **genie** that computes every actual set
exactly, over held-out drops and traffic; in S2, resources at a met target. Also: tool time and tokens to
the submitted design, and **false feasibility** -- a submitted design or policy that misses the target on the
ground truth.

## 5. Arms and fairness

Carried from `plans/dse_cg.md` (its fairness rules and the service, ledger, replay, scoring and agent harness
in `examples/dse_cg/`), plus:

- **Both arms:** the untimed bit-exact model (fast), the hardware pre-built and verified, synthesis and RTL
  simulation (slow), the same budget, the same documentation. **The Waveflow arm also gets pysim.** The
  baseline may write any model of its own -- that is expected (below), and its cost and accuracy are a result.
- **The Waveflow arm must confirm at RTL at least once.** The submit rule: a moderate RTL run (for example
  100 windows) whose mean timing matches the arm's prediction (pysim or the agent's own model) and whose
  outputs are bit-exact. **The tail is judged by the evaluator, offline, charged to neither arm** -- a long RTL
  run per submitted design (sizing in Phase 6). Otherwise one tail confirmation (hours at RTL) would dominate
  both budgets and hide the difference.
- **One policy, both backends.** The policy must run in pysim (on the A53 model) and at RTL (the C++ host of
  the system flow) without the baseline porting it by hand on every run. Options, to choose in Phase 4: the
  policy written once in C++, compiled for the A53 model's kernels and linked into the RTL host; or a
  restricted policy interface (rankings, groupings and iteration counts as data) that both backends execute.
  First see how `SwHost`'s C++ twin is produced (`docs/guide/systems/` *system* pages, the
  `waveflow-system` skill).
- **The baseline's likely strategy, and why it is not a strawman:** predict completion with a proxy --
  untimed iteration counts x cycles per iteration measured once at RTL -- then a queueing model or a small
  simulator. That proxy is good for a pool of independent units with fixed iteration counts. It goes wrong
  with **contention** (a shared memory: LTBF's Shared Mem port was 90-97% busy in dse_cg, decision 7 there)
  and with **data-dependent solve times**. So the engine keeps a shared memory, and (proposed) each job has an
  iteration cap plus a tolerance with early exit (Section 9, item 1). The size of Waveflow's advantage is how
  wrong the proxy is near the deadline: measure it directly.
- **The Waveflow arm's pysim names its platform, and refuses to run without it.** The decision-gap models of
  Section 7 are opt-in by platform (`xczu48dr_250mhz_vitis2025_1`; the user's decision, 2026-11-22, as the FFT's
  calibrated timing is). Naming it is no burden -- a DSE fixes its platform anyway -- but forgetting fails
  silently: pysim would run with zero gaps and read optimistic exactly at the deadline. So the study's engine and
  harness set a **refusal**, not only a label: a pysim run whose blocks lack the platform's timing fails. The
  pattern exists: `VitisFft.require_calibrated` (`waveflow/vitis_l1/hw.py`, on by default). Labels alone
  (dse_cg's `UNCALIBRATED`) were noticed by one agent, not all.

## 6. Simulation speed (why pysim can serve the inner loop)

From `docs/evaluation/sim_speed/`:

| unit | untimed | pysim | RTL under XSI | per-design RTL build |
|---|---|---|---|---|
| `SsrFft`, L = 1024 (`fft.md`) | 1.87 ms a frame | 2.29 ms (flat) | 131 ms (57x pysim) | csynth ~131 s + XSI ~25 s |
| `VecUnit` CG, 8 x n, 1-64 jobs (`vecunit_cg.md`) | 1-4 ms | 28 ms - 1.0 s, cycles within 0.14% of RTL | 4.1-315 s (146-315x pysim) | csynth 114 s + XSI build 154 s |

The untimed model is about as fast as pysim; what pysim adds is the timing, at a small overhead. So the
question is never "which simulator is faster" but **how many candidates the baseline must time at RTL**, and
whether it can time a tail at all.

**But the vector unit's pysim is not yet fast enough for this study's inner loop.** Per CG job, 8 x 32,
from `vecunit_cg.md` (64 jobs: 4.1 ms golden, 472 ms pysim run, 474 ms interpreter):

| per CG job | time |
|---|---|
| the untimed application model (golden) | ~0.06 ms |
| the VecUnit pysim -- it runs the bit-exact instruction interpreter, 99% of its cost | ~7.4 ms |
| a job's transactions through a dispatcher pool (`plans/stream_dispatch.md`: 40 jobs, 3.8 ms; ~6 events at the ~16 µs an event of `sim_speed/index.md`) | ~0.1 ms |

At ~350 jobs a window that is ~2.6 s of pysim a window, and ~7 h for a 10,000-window tail -- per policy
evaluation. Still ~300x faster than RTL, but too slow for an agent to iterate on policies. Precondition 6
(Section 7) fixes it. The dispatcher's ~16 µs an event is a known cost (`sim_speed/index.md`, *What is still
slow*), worth reducing, but not what decides this study.

## 7. Preconditions -- pysim must be right exactly at the deadline

1. **The arbiter's decision gap in pysim.** `StreamArbiter` leaves 3-8 idle cycles between frames at RTL;
   pysim leaves 1 (`docs/guide/streams/arbiter.md`: "pysim does not model this yet").
2. **The dispatcher's decision cycles in pysim.** `StreamDispatch` (built this session, `plans/stream_dispatch.md`)
   measured 3 / 3 / 5 / 5 idle cycles a job at 2 / 4 / 8 / 16 workers first-free, 4 / 5 / 10 / 20 round-robin,
   at 100 MHz, Vitis / xsim 2025.1; pysim spends 0.

   **Items 1 and 2 are in progress** (2026-11-22, on the lab machine, `plans/stream_dispatch.md` Stage 7): one
   timing model for both blocks, the delay a `timeout` a firing (it does not slow pysim: its cost is per burst,
   not per word), calibrated on `xczu48dr` at 250 MHz, **opt-in by naming the platform** (the user's decision),
   the arbiter included (the user's decision: in a pool, which worker takes a job depends on when the arbiter
   returns its credit). Done when pysim's idle cycles between jobs and frames equal the RTL's at every calibrated
   point. **Ask that stage to carry a `require_calibrated` refusal like `VitisFft`'s** (Section 5).
3. **The A53 scheduler's run time** priced by the calibrated platform (`docs/guide/cpu/`), since it eats the 5 ms.
4. **The engine redesigned** (dse_cg's interconnect problem) -- likely a pool of CG units behind a
   `StreamDispatch` with results merged by a `StreamArbiter`, `m_sel` back as the credits
   (`docs/guide/streams/dispatch.md`, `docs/guide/systems/shared_kernels.md` *A pool of workers*), on a
   shared memory for the matrices. Its job descriptor carries the user set and the iteration count (D5).
5. **The shared memory's timing calibrated** -- dse_cg's step 1f brought pysim within 1.5% of RTL at every
   memory setting; keep that.
6. **A fast mode for the CG engine's pysim** (Section 6: the interpreter makes a job ~7.4 ms, ~100x the golden).
   As the FFT's flat pysim (`docs/guide/dsp/ssr_fft/timing.md`, *the flat pysim*): each job's numbers from the
   fast application-level golden, its cycles from the vector unit's calibrated timing model (`VecTiming`,
   `waveflow/vecunit/calib.py`; pysim within 0.14% of RTL), given the iteration count the golden already knows
   (with a tolerance, the iterations it actually ran). Two conditions:
   - **Proved, not assumed:** an equality test against the interpreter-backed pysim -- the same bits, the same
     cycles, at every calibrated configuration -- as `test_flat_pysim_is_the_task_chain` does for the FFT.
   - **Contention kept:** only the arithmetic and the instruction stepping are replaced; the job's memory
     traffic still goes through the shared-memory model, at the times the interpreter would issue it (or a
     calibrated summary of them, proved equal the same way). Without that, the fast mode would erase exactly the
     effect that defeats the baseline's proxy (Section 5).

   Target: ~0.1-0.2 ms a job, tens of milliseconds a window, minutes for a 10,000-window tail.

Without 1-3, pysim is systematically optimistic exactly where the deadline decides; without 6, it is right
but too slow to search with (dse_cg saw pysim read
3-4% fast at its fastest designs, so its agents verified 2-10 designs instead of one).

## 8. Phases (proposed)

| phase | what | done when |
|---|---|---|
| 0 | The open decisions of Section 9 answered by the user; this plan accepted | the decisions recorded here |
| 1 | **The algorithm, untimed (Python):** the user scheduler (fixed), candidate sets per window, R̂(S) for any set S, the closest-superset rule, the no-whitening fallback, the genie, the SINR loss; over the Sionna Denver drops (`examples/ltbf_cg_mem_dse/data/drops/`, cached; numpy only) | the brackets (all exact / none) and two scripted policies scored |
| 2 | **The engine** (Section 7, item 4): the job interface (set, iterations, tolerance), the pool, the shared memory; pysim and RTL; gates asserting counts; then **its fast mode** (Section 7, item 6) | the engine at RTL bit-exact against pysim, its cycles within the targets; the fast mode equal to the interpreter-backed pysim, bits and cycles, and a window in tens of ms |
| 3 | **Preconditions 1-3** (pysim's decision gaps -- `plans/stream_dispatch.md` Stage 7, in progress; the A53 pricing of a scheduler); the `require_calibrated` refusal in the study's harness | pysim's idle cycles equal RTL's at every calibrated point; an uncalibrated run refused |
| 4 | **One policy, both backends** (Section 5) | a scripted policy runs unchanged in pysim and at RTL, the same RBs covered |
| 5 | **Scripted baselines:** greedy coverage by probability; insurance superset first; iteration-count sweeps. The agent must beat these | their scores on held-out windows |
| 6 | **Sizing.** Ground truth: the evaluator's long RTL runs for a tail (windows needed for a p99 at the target's precision, RTL hours a design). The agent's side: pysim's wall time a window with the fast mode, so a policy evaluation fits the budget (Section 6: ~7 h a 10,000-window tail without it) | the cost a submitted design and a policy evaluation, and whether both are affordable |
| 7 | **The agent harness:** reuse `examples/dse_cg/` (service, ledger, `run_agent.py`, `score.py`, sandbox); the task text, rubric and objectives fixed in **waveflow-eval** (Section 9, item 6) before any run | pilots valid in both arms |
| 8 | **S1, then S2:** pilots, pre-registration (budget, model, effort, k, N), N runs per arm | scored from the ledgers |

## 9. Open -- to settle with the user before Phase 1

1. **D5's reading**, and **fixed iterations or a cap plus a tolerance.** Fixed iterations make a job's time
   nearly deterministic (only set size and contention vary), which helps the baseline's proxy. A cap plus a
   tolerance with early exit makes it depend on the channel's conditioning -- closer to how CG is run, and
   harder to predict without timing. *Proposed:* cap plus tolerance, the shared memory kept.
2. **What R̂ is exactly**, in LTBF's terms -- `Q̂(S) = I + Σ_{j∈S} Ĥα,ⱼĤα,ⱼᴴ` per set and the CG against it,
   or a whitening of a different form -- and its dimensions.
3. **Sizes:** antennas, users, RBs, candidate sets a window, the largest set.
4. **The user scheduler's output:** sets with probabilities, or sets only; the traffic model behind it (a
   proportional-fair scheduler in Python is the simplest realistic source).
5. **Deadline granularity:** once per 5 ms window, or per slot inside it; how long a matrix stays valid
   (reuse across windows, warm start from an earlier window's matrix).
6. **Hygiene for the held-out task.** This plan describes the problem, so the agents under test must run in a
   sandbox without `plans/` and `docs/evaluation/` (as dse_cg's do: `brief.build_sandbox`). The agent-facing task
   text, the rubric and the objectives go in the private evaluation repository `sdrangan/waveflow-eval` (its
   rules: a canary line in every file, a SHA-256 in `HASHES.md` before any run, never copied into this
   repository).

## 10. Files to read

- `plans/dse_cg.md` -- the service, the fairness rules, G1, the pilots and the tight-deadline experiment (its
  progress log, 2026-11-07 to 11-12); `examples/dse_cg/` (`service.py`, `score.py`, `run_agent.py`, `brief.py`,
  `prereg.py`, `policies.py`); `docs/guide/dse/`.
- `plans/ltbf_dse/ltbf_dse.md` (Sections 1.1-1.4: the algorithm, the architecture, the scheduler) and
  `examples/ltbf_cg_mem_dse/`.
- `plans/dse_arch.md` -- the architecture-exploration plan; its review (Section 11 D) says what is stale.
- `plans/toy_shared_memory.md` -- question 3 (do baseline agents write their own simulator; how good is it)
  is the same question this study asks on a real design.
- `plans/stream_dispatch.md`, `docs/guide/streams/dispatch.md`, `docs/guide/systems/shared_kernels.md`.
- `docs/evaluation/sim_speed/` (`fft.md`, `vecunit_cg.md`), `docs/evaluation/agent_skills/shared_kernel.md`.
- `docs/guide/cpu/` -- the A53 model.

## 11. Other conclusions of the same conversation (not this study)

**A. Comparing Waveflow with no Waveflow on a build task** (the delay-Doppler held-out problem,
`waveflow-eval/heldout/shared_kernel_01`, now retired). The existing runs do not make that comparison: the
Vitis-only arm was one chat reply ("do not write files", uncompiled C++), the Waveflow arm had a build run
that stopped at pysim, and the rubric names Waveflow blocks. What would:
- **One finish line, graded by a hidden testbench:** the spec fixes the RTL interface (AXI-Stream ports, word
  packing); the evaluator writes a cocotb or XSI acceptance testbench and a numpy golden neither arm sees.
- **Hidden tests:** staggered and bursty arrivals, backpressure, edge parameters, a long soak; counts and
  `tx_id`s asserted on every output.
- **Metrics:** milestones (compiles, csim, csynth, passes hidden RTL tests); correctness against the golden;
  **false confidence** (the agent says done, the hidden tests fail); time, tokens, interventions; FFT
  utilisation, throughput, resources, Fmax.
- **Arms:** A Vitis HLS + numpy + cocotb; B as A plus the shared-kernel guidance rewritten neutrally (does
  advice alone close the gap?); C Waveflow without skills; D Waveflow without the reference example; E full
  Waveflow. 5+ runs an arm, a fixed budget, fresh problems, a neutral rubric, the lab machine for every arm.
- **The instruction to both arms (user's question):** "verify the design as you see fit before submitting;
  graded by a hidden testbench at this interface". Give the scenario classes in general terms and **one public
  smoke test** (one job, its expected output), so interface misreadings don't dominate. Do not tell the
  baseline *how* to verify -- that is the treatment. Ask each agent to report what it verified and its
  confidence, for the false-confidence score.
- **Report the infrastructure cost** (Stages 0-6 of `plans/shared_kernel.md`, about 8 hours) as a one-time cost
  against per-design savings.

**B. A second candidate problem: multi-channel spectrum sensing with confirmation.** C independent channels
(frequency sub-bands); per block a scan FFT, |X|², CFAR at a lenient threshold; each detection triggers a
confirm pass on the shared FFT (a matched filter, X·conj(T) then an IFFT, per template). Input overflow drops
samples, so timing changes the answer. Axes: FFT instances (one shared, scan and confirm, one per channel, or
a pool via `StreamDispatch`), FFT width, arbitration (confirm or scan first), confirm buffering, a cap on
confirms a block, depths, formats, the CFAR threshold. Weaker than the matrix scheduler for the simulator
question: the FFT's service time is fixed.

**C. The delay-Doppler problem made fully parametric** (every architecture pre-built): little Waveflow
advantage unless the objective has a tail. It does have non-monotone couplings -- FFT width against antennas
in flight under a BRAM limit (Little's law: a wider FFT needs more antennas in flight to stay busy, each with
resident buffers), speculative against on-demand gradient frames (depends on the acceptance rate), the theta2
grid against iterations, throughput against p99 latency -- but a capable baseline estimates most of them from
the untimed model's counts. At L = 1024 and 44 FFT frames a job (the build run's 308 frames over 7 jobs): about
10 min of RTL plus 2.5 min of build per design for a mean over 100 jobs, about 16 h for a tail over 10,000.

**D. `plans/dse_arch.md` is stale** (written 2026-11-05). To fix when it is next edited: its primitives
`CgVectorCore` / `SystolicCore` are retired (use `SystolicEngine`, `VecUnit`); it misses `StreamArbiter`,
`StreamDemux`, `StreamFramer`, `StreamDispatch`, `SsrFft(direction="runtime")`, the memory and host-bus
adaptors and the A53 model; "V1 and V2 already exist" is wrong (V1 is now the LTBF system, V2 is designed in
dse_cg and not built); its dependency on dse_cg Phases 1-3 is met; its open question 4 (host time) is answered
by the A53 model; its architecture IR overlaps frames, composites, `HwSystem` and the dse_cg adaptor; its
traditional arm's IPI renderer is expensive (a cheaper arm composes the same HLS sources with `hls::task`);
it should require waveflow-eval's hygiene. This plan supersedes its E0/E1 as the first concrete study.

**E. Built during the conversation:** `StreamDispatch` (`plans/stream_dispatch.md`) -- one job stream over a
pool of identical workers, credits back as the merging arbiter's `m_sel`; pysim, HLS body, four RTL gates
passing at 2025.1, resources filed, 250 MHz met on the RFSoC. Its decision cycles in pysim are its Stage 7.
