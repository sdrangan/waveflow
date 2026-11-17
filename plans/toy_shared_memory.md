# Plan: a toy experiment on the value of a calibrated simulator

**Status: DRAFT, not started, written 2026-11-14, revised 2026-11-16.** Follows the agentic DSE study
of `plans/dse_cg.md` and its tight-deadline experiment, and reuses its service, ledger and scoring where
it can (S0, Q2). It is the next experiment, and it gates `plans/dse_arch.md`: this plan isolates one
question on one small design before any architecture search (§9).

## 0. Why this experiment

`dse_cg` found the parametric problem easy (Progress log, 2026-11-12). All six pre-registered pilots and
all 36 tight-deadline runs submitted the measured optimum. The baseline agents learned the space from
`golden`, `hls_estimate`'s relative sizes and a few RTL runs (median 9 a run). The simulator arm used
1.4-3.5x less tool time in the pilots and 1.1-1.8x on tight deadlines, before calibration. With the
calibration charged up front (10.6 h) the baseline wins every objective; amortized over the runs the
arms are about even.

The reason: on that grid a few measurements predict the rest. Time per job is smooth in the parameters,
so an agent can interpolate. A simulator pays off when the quantity that decides the design cannot be
measured cheaply and cannot be interpolated from a few measurements. A tail probability under random
contention is such a quantity: one RTL estimate at ε = 10⁻⁴ takes hours (S0, Q3), and the tail moves
sharply with the parameters near the limit.

## 1. Goal

Learn, on the simplest problem that can show it, whether a fast, calibrated,
bit-exact simulator helps a frontier agent beyond what the agent achieves with
the standard tools and its own ability to write code.

The task is a tail-probability requirement on a design that shares memory with
random traffic. Only long simulation can check such a requirement, and RTL
simulation is too slow to check it for more than a few designs.

Everything in this plan runs on a simulated-tools harness: a hidden "true"
model stands in for synthesis and RTL simulation, and a virtual clock charges
realistic tool times. Where a part of the toy exists in Waveflow or the AMD
IP, the true model's numbers (timing, arbitration, resources, tool times) are
measured on the lab machine (S0b); the rest are made up and say so. Building
the toy itself as hardware is future work (§8).

So the experiment measures the value of *a* calibrated simulator, at a chosen error, not of Waveflow's
pysim itself. It says something about Waveflow through one link: the error at which the simulator's
advantage disappears (S5's sweep), against the error Waveflow's pysim actually reaches. On the CG system
pysim's cycles were within 1.44 % of RTL (mean absolute, G1, Vitis 2025.1). A tail is more sensitive
than a mean, so the error knob is set in timing terms and its effect on the tail is reported (§3).

The questions, in order:

1. Can we write a standalone simulator by hand that estimates the tail well
   enough to pick the right design? At what cost?
2. Does an agent with PySim submit better designs, or reach them sooner, than an
   agent with the standard tools?
3. Do baseline agents write their own simulator? How accurate and how
   consistent are their models?

## 2. The toy

- **DUT:** samples arrive at a fixed rate into an input FIFO. The DUT processes
  them in blocks and writes the results to a banked shared memory.
- **Background traffic:** a generator issues bursts to the same memory, with
  heavy-tailed on-periods.
- **Arbiter:** round-robin or weighted, with finite FIFOs on both sides.
- **Data-dependent stage (a switch):** when on, the DUT's write volume or
  compute time per block depends on its input, for example a compression stage
  whose output length depends on the samples, or an iterative stage with early
  exit.
- **Requirement:** P(input FIFO overflow per block) ≤ ε at a given background
  load, with ε between 10⁻³ and 10⁻⁴.
- **Design parameters:** FIFO depth, number of banks, address mapping, burst
  length, arbiter weight, DUT parallelism. Four to eight options each.
- **Objective:** minimize BRAM, then LUTs, subject to the overflow limit.

Keep it small: one DUT, one traffic source, one memory.

**The space must not be monotone.** If one knob (FIFO depth) buys feasibility on its own, an agent
over-provisions it and lands near the optimum without any tail estimate. The cheapest feasible design
should need a combination: for example banks and address mapping that remove conflicts, so a shallower
FIFO suffices, against a deeper FIFO on one bank. S3's shortcut test checks this.

**Microarchitectural effects in the true model.** An agent's own simulator goes wrong where the hardware
does something its description does not say. Put in the true model the effects `dse_cg` found only by
tracing RTL (Progress log, 2026-11-07, step 1f), at the sizes measured there and
re-measured at the toy's options by S0b (L2, L3):

- each master's transfers split into 16-beat bursts, each burst holding the slave on its own;
- a turnaround of one cycle between back-to-back bursts (2n + 2 cycles a read burst, not 2n + 1);
- a writer that waits for each burst's response before the next;
- bank conflicts set by the address mapping, not only by the number of banks.

None is hidden on purpose: an engineer with the RTL could find each one. They are what a short RTL run
shows only in aggregate, and what moves a 10⁻⁴ tail.

## 3. The simulated-tools harness

The hidden true model is an event-driven Python model of the toy. It lives
outside the agent's sandbox, and nothing the agent can read reveals its
formulas. The agent's tools are views of it:

| Tool | What it returns | Charged |
|---|---|---|
| Untimed model | The DUT's bit-exact output, so the data-dependent quantities | per block, small |
| HLS estimate | Per-block resources and latency, no contention | about 1 min |
| Synthesis | True resources plus small noise | about 6 min |
| RTL simulation | True cycles and overflow events for the requested length and seed; optionally a per-transaction trace (each burst's start, end and port), as an engineer would take from a waveform | about 160 s to build, then about 1,800 cycles/s |
| PySim (simulator arm only) | The true model with a tunable error: a timing bias, a spread, one effect of §2 left out | about 5×10⁵ cycles/s |

The tool rates are the CG grid's medians (S0, Q3) until S0b's L1 and L5
measure them at the toy's size. They stay parameters of the harness.

The harness reuses the service's ledger, cache and scoring through a new
backend (S0, Q2). The virtual clock is event-driven over 2 worker
slots, so a batch is charged by when the slots free up. Results return at once.

**PySim's error knob.** Set in timing terms (a bias on each burst's service time, a random spread on it,
or one effect of §2 left out), since that is how a calibrated model errs. For each setting, report the
error it causes in the overflow probability near the limit, from long runs of both models. The
mean-cycle error and the tail error are both reported; only the first is comparable with `dse_cg`.

**Calibration charge.** A real PySim has to be calibrated, and in `dse_cg` that charge decided the
result. The harness charges the simulator arm a calibration time, a parameter, reported three ways as in
`dse_cg`: up front, amortized over the arm's runs, and as the break-even charge at which the arms tie.
The default is what calibrating the toy's parts would take at the CG grid's rates: the memory's traced
runs (688 s in `dse_cg`), the crossbar's shapes, and a sweep of the DUT stage; a first guess is 1-2 h,
replaced by the charge S0b's L5 measures. Calibration is
per part, so a part already calibrated for another design costs nothing; the break-even charge says how
much reuse the result needs.

**What the agents are told.** Both arms get the same brief: the block diagram, the parameters and their
costs per option, the requirement, the tools and their charges, and a datasheet for each part that says
what a vendor's documentation would say (the arbiter's policy, the burst length, the memory's latency),
not the turnaround or the writer's response wait. The simulator arm's brief adds PySim and its stated
accuracy. The baseline may write any code.

## 4. Arms and scoring

| Arm | Tools |
|---|---|
| Baseline | Untimed model, HLS estimate, synthesis, RTL simulation. It may write any code in its sandbox, including its own simulator. |
| Simulator | The same, plus PySim. |

- **Submit rule:** no verification gate. An agent submits one design with its
  own estimate of the overflow probability and the evidence for it, as an
  engineer signs off. The harness judges it by a long run of the true model.
  Gating on an RTL tail estimate would cost hours a design and hand the
  baseline the very measurement the experiment is about. A submission that
  misses the limit scores as no submission.
- **Primary score:** resources of the submitted design, among designs that
  truly meet the limit (judged by a long run of the true model), at a fixed
  budget of tool time.
- **Secondary score:** tool time to the first design within 5 % of the best
  design that meets the limit.
- **Recorded per run:** whether the agent wrote its own model; that model's tail
  estimate against the truth; turns, tokens and tool time spent building it;
  whether the agent checked its model against RTL; whether the submitted design
  misses the limit.

## 5. Steps

### S0. Questions before building, and their answers

Four questions decide how much work S1 is and whether the toy's central
assumption holds. Q1, Q2 and most of Q3 were answered on 2026-11-14 by reading
`main` (at `3165c45`) and Ali's branch `paper/ltbf_cg_dse_agentic` (at
`c26043a`), and the CG grid's measurements. What remains for Ali is at the end
of this step.

**Q1. Which parts of the toy does Waveflow already model?**

| Part of the toy | In Waveflow | Where |
|---|---|---|
| Input FIFO that never stalls and counts drops | Yes. The framework's sample buffer between a converter and a design: its ingress never stalls, and pysim counts dropped samples. A pysim twin of hand-written RTL. | `waveflow/hw/rf_samp_buf.py` (`RfSampBufRx`) |
| Banked shared memory | Yes. A memory model with `banks` and `mem_rate`, each bank on its own crossbar port, used by the CG system. At RTL it is the framework's behavioural memory, so its block RAM comes from a formula. | `examples/dse_cg/ltbf_cg_mem.py` on Ali's branch; `waveflow/hw/memory.py` |
| Crossbar and contention | Yes. `AXIMMCrossBarIF` routes masters to slaves; each slave port is a SimPy resource per direction, with a calibrated bus-occupancy model (`BusTiming`). The AMD crossbar IP's area and latency are measured per shape. | `waveflow/hw/memif.py`; `examples/xbar_profile/` |
| Arbiter policy | Partly. Contending masters are served in request order (a SimPy resource). There is no round-robin or weighted option. | `waveflow/hw/memif.py` (`MMIFSlave`) |
| Random background traffic | No. No traffic-generator module was found. | |
| Data-dependent stage | No ready-made one for this toy. | |

*Choice:* build the true model from these units: `RfSampBufRx` as the DUT's
front end, the CG system's banked memory behind `AXIMMCrossBarIF`. Write two
small new pieces: a traffic generator (an `MMIFMaster` issuing bursts with
heavy-tailed on-periods) and a data-dependent stage. Make the arbiter's policy a
parameter (a priority resource) only if S2 shows request order is too easy.
Since the true model and PySim share these units, PySim's error is added on
purpose (§3), and the hand-written simulator in S2 uses none of them.

*Note for later:* the AMD crossbar IP has its own arbitration, which pysim's
request-order model may not match. In Part A that mismatch is a natural choice
for the "effect left out" of PySim's error. With real tools it is a calibration
item.

**Q2. Can the service answer calls from a model instead of the measured
grid?** Yes. `examples/dse_cg/service.py` (Ali's branch) talks to its backend
through one method, `call(what, *args) -> (row, cost_s)`; `ReplayBackend` (the
grid) and `LiveBackend` (the tools) both implement it. The service itself keeps
the ledger, the cache by identity, the 2 worker slots on a virtual clock, the
budget and the submit rule.

*Choice:* add a third backend, `ModelBackend`, that answers from the true model
and returns the charged times of §3, and a design adaptor for the toy
(a `DseAdaptor` subclass: `space`, `golden`, `pysim`, `synth`, `rtlsim`,
`hls_estimate`, `identities`, `sim_identity`). Three things need small changes
or additions, not a refactor:

- *Run length and seed.* A tail estimate is an RTL run of a chosen length. The
  workload carries the number of blocks and the seed, so a longer run is a new
  simulation identity, and `rtlsim`'s charge (build once per build, then
  `simulate_s`) follows from `ModelBackend`'s `simulate_s = cycles / rate`.
- *Submit rule.* Today a design is accepted if it was synthesized and
  RTL-simulated on the objective's workloads. For the toy there is no gate:
  the submission carries the agent's estimate and evidence, and the true
  model judges it (§4). The service needs a `submit` that skips the check
  and records the estimate.
- *Scoring.* `score.py` scores time to a known optimum. The toy's primary score
  is the resources of the submitted design among designs that truly meet the
  limit. `score.py` already has an anytime curve (`anytime_curve`), which the
  secondary score can use.

**Q3. How fast are RTL simulation and PySim?** For the CG system, from the
grid's 247 RTL runs and 768 PySim runs (`data/g1/` in the paper repository):

| | Median | Range |
|---|---|---|
| RTL simulation | 1,800 cycles/s, plus about 160 s to build | 780–3,200 cycles/s |
| PySim | 5.5×10⁵ cycles/s (about 1.8 s a design) | 1.4×10⁵–9.1×10⁵ cycles/s |

So PySim is about 300× faster than RTL on the CG system. At ε = 10⁻⁴ a tail
estimate needs about 3×10⁷ cycles: about 4.6 h of RTL, about a minute of PySim.

*Choice:* use these rates in the harness (§3), and also run it at 100× and
1,000× (S5). The toy is a much smaller design, so its RTL may run several times
faster per cycle, and its PySim cost depends on how many events a cycle needs.
If the toy's own ratio comes out below about 30×, speed alone won't separate the
arms; the S6 note says so and the user decides whether to go on. If RTL is fast
enough that one tail estimate takes less than a quarter of the 8-h budget, make
ε smaller or the blocks longer.

**Q4. Is the DAC-period work merged into `main`?** Yes, since 2026-11-16:
`paper/ltbf_cg_dse_agentic` merged as PR #311, so the service's backends, the
calibration and integration code, `ltbf_cg_mem.py` and `deadline.py` are in
`examples/dse_cg/` on `main`. Two related branches also exist,
`paper/ltbf_cg_mem_dse` and `paper/ltbf_cg_seq_dse`; nothing here needs them.

*Choice:* start S1 from `main`.

**Still open** (none blocks S1; the harness takes each as a parameter):

1. How fast does XSI simulate a design the toy's size? Measured by S0b, L1.
2. Does the AMD crossbar IP arbitrate round-robin, and does pysim's
   request-order model match it? Measured by S0b, L2.
3. For Ali: should the two other `paper/` branches be merged? (The first half
   of this question, `paper/ltbf_cg_dse_agentic`, is answered: PR #311.)

**Done when:** the choices above are confirmed or changed. S0b's answers are
copied here when they arrive.

### S0b. Lab calibration (the lab machine, unattended)

The true model's numbers are made up only where nothing real exists. The
toy's other parts exist in Waveflow and the AMD IP, so measure them on the lab
machine (Vitis HLS / Vivado 2025.1, `xczu48dr` licensed) while S1 is written.
The traffic generator and the data-dependent stage do not exist as hardware;
they stay made up (plausible numbers, written in S1 with their source).

This step runs in a Claude Code session on the lab machine, without the user.
What that session must know:

- **Launch long jobs detached** (`setsid`, its own session, as
  `~/ali/dse_cg/g1_resume.sh` did). A session's background task dies with the
  session: G1 lost 12.7 h that way (`dse_cg` Progress log, 2026-11-09).
- **Load matters to tool time.** In G1 a run beside other jobs took up to 1.8x
  as long. Run L1's timed runs at a stated condition (alone, or beside exactly
  one other RTL run, as the harness's 2 slots assume), and log anything else
  that ran.
- Each task writes its results to `~/ali/toy/lab/<task>.json` with the tool
  version, the part, the commit and the date; each is resumable and skips what
  is recorded. Keep build paths short.
- **No change to the framework without the user.** A new probe or a longer
  workload in an example is fine on a branch; a change under `waveflow/` is
  written down and waits.
- When done, write a short note `~/ali/toy/lab/NOTE.md`: what ran, what
  failed, the tables of L1-L5, and anything surprising. Then stop.

**L1. RTL simulation speed at the toy's size** (answers Ali's Q1). For three
existing designs near the toy's size, record the XSI build time, the cycles
simulated, the simulation time and cycles per second, alone and beside one
other RTL run:

- `examples/rf_samp_buf_rx` through the system flow
  (`rf_samp_buf_rx_system_build`): the sample buffer, the toy's front end;
- `examples/mem_copy` (`mem_copy_system_build`): `MemRStream` and `MemWStream`
  on the behavioural memory;
- the 2 x 1 crossbar of `examples/xbar_profile`: two masters on one memory.

Lengthen each workload to at least 10⁶ cycles (the gates run a few thousand,
too short to time); three lengths per design, so build and per-cycle costs
separate. *Output:* build s and cycles/s per design and condition. This sets
the harness's RTL rate (§3) in place of the CG system's 1,800 cycles/s.

**L2. Crossbar arbitration under sustained contention** (answers Ali's Q2).
`xbar_profile`'s `same_w16` probe has every master write at once, one burst
each. Add probes where the two masters of a 2 x 1 crossbar (SAMD and SASD,
64-bit) each issue 8 bursts of 16 beats back to back, then with master 1
starting 1, 5 and 17 cycles late, and with mixed reads and writes. Read the
grant order and each burst's start and end from the VCD (count
`VALID && READY`; the BFM's counters are not the wire). Run the same traffic
in pysim (`AXIMMCrossBarIF`, request order). *Output:* the grant sequences,
whether the IP alternates (round robin) or serves in request order, the gap
cycles at each switch of master, and pysim's error per probe.

**L3. Memory burst timing at the toy's options.** The toy's banks (1, 2, 4)
and burst lengths (4, 8, 16, 32 beats) on the behavioural memory, at BRAM rate
and one-cycle rate. Reuse `dse_cg`'s flow from step 1f (the device harnesses
on Shared Mem traced, `fit_turnaround` in `waveflow/calib/mem_timing.py`):
record cycles per burst, the turnaround, and the writer's wait for each
response. *Output:* a table per (banks, rate, burst length), and whether
`dse_cg`'s numbers (2n + 2 a read burst, 21 cycles a 16-beat write) hold.

**L4. Resources per option.** Out-of-context synthesis at `xczu48dr`:

- the sample buffer at depths 256 to 8192 words (the toy's FIFO depth);
- the crossbar shapes 2 x 1, 2 x 2, 3 x 1 at 64 and 128 bits (the existing
  `xbar_profile` records are at the stand-in `xczu7ev`; rerun at `xczu48dr`);
- `MemRStream` / `MemWStream` at the burst lengths of L3.

The memory's block RAM comes from a formula (`dse_cg` decision 2), so it needs
no synthesis. *Output:* LUT, FF, DSP and BRAM per option: the cost table of
the true model.

**L5. Tool times and the calibration charge.** From L1-L4's logs: csynth,
synthesis and XSI build seconds per part and option, which set the harness's
charges (§3). Then the calibration charge: the tool-seconds of the runs a real
PySim would need for the toy's parts (L2's probes, L3's traces and a subset of
L4), listed run by run as `examples/dse_cg/calibration.py` does. This
replaces the 1-2 h guess of §3.

**Done when:** `NOTE.md` and the five result files are on the lab machine,
and the user has copied the tables into this plan (S0's answers and §3).

### S1. True model and harness

Build the true model and the harness of §3.

**Done when:** a long run of the true model gives an overflow probability with a
confidence interval for any design; the harness answers each tool call and
charges the virtual clock; the ledger reproduces a scripted call sequence
exactly.

### S2. Hand-written standalone simulator

Before any agent run, write the simulator a careful engineer would write, in
two versions:

- **S2a, queue only:** FIFO, arbiter, banks and traffic, with parameters fitted
  from short RTL runs.
- **S2b, with data statistics:** S2a plus the data-dependent quantities
  collected from the untimed model.

Record the build cost: hours, lines of code, and the number and tool time of
the RTL runs used to fit it. Compare each version's tail estimates with the
truth near the limit, and check whether it picks the same design.

**Done when:** a table lists, for S2a and S2b with the data-dependent stage off
and on, the build cost, the tail error near the limit, and whether the chosen
design matches the true best.

**Decision:** if S2a picks the true best design with the data-dependent stage
on, the toy is too easy. Strengthen the data dependence or the effects of §2
and repeat S2 before going on. If S2b picks it (data statistics combined with
a timing-only queue model by Monte Carlo), the split that S7 is designed to
break works on this toy: consider moving S7's coupled DUT into S4-S5 as the
main design, and decide with the user.

### S3. Sanity checks

- With PySim's error set to zero, a scripted simulator policy beats a scripted
  baseline policy on every objective.
- **Shortcut test:** a baseline agent given the task without tools writes down
  its shortlist. If the list is short and right, the toy is too easy; return
  to S2.

**Done when:** both checks pass and their outputs are saved.

### S4. Objectives

Choose 3 objectives (ε and background load) where S2 and S3 show that the
choice of design depends on the tail.

**Done when:** the objectives and their true best designs are written in this
plan.

### S4b. Pilots and pre-registration

As in `dse_cg` step 7: one agent run per arm per objective, then read every
transcript. Check that the brief is enough (what the agents say they lacked),
that no agent works around the tools, that runs end at their submit, and how
many turns and how much wall time they take. Set the caps from the pilots
(`dse_cg`'s rule: about 3x the pilots' maximum). Then freeze, before any scored
run: the objectives, the budget, the calibration charge, PySim's error
settings, the model and effort, N, and the caps.

If every pilot of both arms submits the true best design, stop: the toy is too
easy, as the CG grid was. Return to S2.

**Done when:** the pilots' records are saved and the pre-registration is
committed with a date.

### S5. Agent runs

10 runs per arm per objective, with the data-dependent stage on, with Claude at
high effort and a fresh sandbox each run. Then repeat the simulator arm at PySim
errors of 1, 3 and 10 %. Also run the scripted policies of S3 on each objective,
as a floor for both arms.

**Done when:** all runs are scored from their ledgers and the per-run records of
§4 are complete.

### S6. Report

A short internal note in the style of the tight-deadline note:

- the scores of both arms per objective;
- how many baseline agents wrote their own model, and its accuracy against S2
  and against the truth;
- the spread of the tail estimates across the agents' models;
- how often submitted designs miss the limit, per arm;
- the PySim error at which the simulator arm's advantage disappears, in both
  timing and tail terms, against pysim's error on the CG system;
- the result with the calibration charged up front and amortized, and the
  break-even charge.

**Pause here** and decide the next step with the user.

### S7 (follow-up). A spectrum-sensing receiver where data and timing interact

Only after S6, and only if the user decides to go on. It reuses S1's harness and
S2's method, and repeats S3–S6 on a richer DUT.

**Why.** In the S1–S6 toy, overflow depends only on timing. A smart agent can
split the problem: a timing-only queue model for the drops, the untimed model
for the data, combined by Monte Carlo. This variant makes the data change the
timing, so that split gives the wrong answer.

**The DUT.** The receiver takes an FFT of each frame and forms a short-time
PSD. It averages the PSD over a band and across frames, and declares a
detection when the average crosses a threshold. Frames the input FIFO drops are
missing from the average.

**The two-way coupling.** When a detection fires, the receiver extracts the
detected band (or runs a finer FFT on it) and writes the result to the shared
memory. That traffic arrives exactly when a signal is present, so frames are
dropped more often exactly when they matter:

> signal → detection → extraction writes → contention → overflow → dropped
> frames → thinner average → miss.

Signals that arrive with bursts of background traffic make the correlation
stronger.

**The requirement.** P(miss) ≤ a target of about 10⁻² to 10⁻³, over a set of
signal scenarios (bandwidths, levels, arrival patterns). A miss has two causes:
an FFT too coarse to resolve the signal, or too many dropped frames.

**Design parameters,** added to the toy's FIFO depth, banks and burst length:
FFT size, averaging length, detection threshold, and the extraction's buffer
and burst size. A larger FFT resolves narrower signals but takes longer per
frame and uses more memory, so the best size lies in the middle and moves with
the background load.

**What to measure, beyond S6's list:**

- the true P(miss) against the estimate from the decomposition above (drops
  simulated without the signal, then combined with the detector); the gap is
  the error an agent makes by assuming drops and signals are independent;
- whether baseline agents assume independence, or simulate data and timing
  jointly;
- the build cost and accuracy of the hand-written simulator of S2 for this DUT,
  in both forms (decomposed and joint).

**Reuse.** Waveflow's FFT examples (`examples/ssr_fft`, `examples/vitis_fft`)
are the starting point for the FFT stage.

**Done when:** the S6-style note covers this DUT, including the independence
gap, and the user has decided what comes next.

## 6. What would count as a clear result

- **PySim helps:** at PySim errors of 3 % or less, the simulator arm submits at
  least 25 % less BRAM, or reaches the target at least 2× sooner, on most
  objectives, and its designs miss the limit no more often. The advantage must
  survive the calibration charge amortized over the arm's runs; `dse_cg`'s did
  not survive it up front, so report that view too.
- **Agents match it:** baseline agents reach comparable designs by building
  their own models. Then the note reports what those models cost to build, how
  far apart they are, and how often they were wrong. That is a result too.

Both thresholds are first guesses, to revisit after S3.

## 7. Cost

API cost about $0.50 per run, so about $50 for S5's 60 core runs and about $100
with the error sweep. The main cost is the time to build S1 and S2.

## 8. Future work (not in this plan)

Only after S6, and only if the toy is informative:

- build the toy in Waveflow and run the experiment with real tools;
- measure how the cost and error of agent-built models grow with system size;
- measure drift when the design changes;
- ask an agent to build a simulator from a summary of Waveflow's requirements;
- the larger CG design space with local buffering;
- Waveflow as a platform for training and testing agents.

## 9. Relation to `dse_arch`

`plans/dse_arch.md` (architecture search by composing calibrated blocks) waits
on this plan. Its primary experiment, E1, needs workloads where system timing
cannot be reasoned out from a few measurements, which is what this toy tests.
If the simulator arm wins here, E1's workloads should carry the same feature
(contention and a tail requirement), and `ModelBackend` may score architectures
off its measured catalogue (`dse_arch` §7). If agents match it here, `dse_arch`
needs a different argument before it is built. V2, the descriptor-ring CG
system (`dse_cg` step 8), is not needed for this plan.
