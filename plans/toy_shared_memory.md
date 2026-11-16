# Plan: a toy experiment on the value of a calibrated simulator

**Status: DRAFT, not started, written 2026-11-14.** Follows the agentic DSE study
of `plans/dse_cg.md` and its tight-deadline experiment, and reuses its service, ledger and scoring where
it can (S0, Q2). Separate from `plans/dse_arch.md`: this plan isolates one
question on one small design before any architecture search.

## 1. Goal

Learn, on the simplest problem that can show it, whether a fast, calibrated,
bit-exact simulator helps a frontier agent beyond what the agent achieves with
the standard tools and its own ability to write code.

The task is a tail-probability requirement on a design that shares memory with
random traffic. Only long simulation can check such a requirement, and RTL
simulation is too slow to check it for more than a few designs.

Everything in this plan runs on a simulated-tools harness: a hidden "true"
model stands in for synthesis and RTL simulation, and a virtual clock charges
realistic tool times. Building real hardware is future work (§8).

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

## 3. The simulated-tools harness

The hidden true model is an event-driven Python model of the toy. It lives
outside the agent's sandbox, and nothing the agent can read reveals its
formulas. The agent's tools are views of it:

| Tool | What it returns | Charged |
|---|---|---|
| Untimed model | The DUT's bit-exact output, so the data-dependent quantities | per block, small |
| HLS estimate | Per-block resources and latency, no contention | about 1 min |
| Synthesis | True resources plus small noise | about 6 min |
| RTL simulation | True cycles and overflow events for the requested length | about 160 s to build, then about 1,800 cycles/s |
| PySim (simulator arm only) | The true model with a tunable error: a timing bias, a spread, one effect left out | about 5×10⁵ cycles/s |

The tool rates are the CG grid's medians (S0, Q3), not the toy's: the toy is a
smaller design, and its rates are still unknown. They are parameters of the
harness.

The harness reuses the service's ledger, cache and scoring through a new
backend (S0, Q2). The virtual clock is event-driven over 2 worker
slots, so a batch is charged by when the slots free up. Results return at once.

## 4. Arms and scoring

| Arm | Tools |
|---|---|
| Baseline | Untimed model, HLS estimate, synthesis, RTL simulation. It may write any code in its sandbox, including its own simulator. |
| Simulator | The same, plus PySim. |

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
  RTL-simulated on the objective's workloads. For the toy, the RTL run must be a
  seed-matched check of PySim or of the agent's own model, not a full tail
  estimate. Decide the rule in S1 and write it here.
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

**Q4. Is the DAC-period work merged into `main`?** Not yet. `main` has an
earlier `examples/dse_cg/`; the service's backends, the calibration and
integration code, `ltbf_cg_mem.py` and the tight-deadline work are on
`paper/ltbf_cg_dse_agentic`. Two related branches also exist,
`paper/ltbf_cg_mem_dse` and `paper/ltbf_cg_seq_dse`.

*Choice:* start S1 from `main` once Ali's merge lands. Until then, start from
`paper/ltbf_cg_dse_agentic` and rebase onto `main` later.

**Still for Ali:**

1. Roughly how fast does XSI simulate a design the toy's size (a sample buffer,
   a small block, a 2-bank memory and a crossbar)? Is it far faster per cycle
   than the CG system's 1,800 cycles/s?
2. Does the AMD crossbar IP arbitrate round-robin, and does pysim's request-order
   model match it closely enough in the CG runs?
3. When will `paper/ltbf_cg_dse_agentic` be merged into `main`, and should the
   two other `paper/` branches be merged too?

**Done when:** Ali's three answers are written here, and the choices above are
confirmed or changed.

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
on, the toy is too easy. Strengthen the data dependence or the traffic quirks
and repeat S2 before going on.

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

### S5. Agent runs

10 runs per arm per objective, with the data-dependent stage on, with Claude at
high effort and a fresh sandbox each run. Then repeat the simulator arm at PySim
errors of 1, 3 and 10 %.

**Done when:** all runs are scored from their ledgers and the per-run records of
§4 are complete.

### S6. Report

A short internal note in the style of the tight-deadline note:

- the scores of both arms per objective;
- how many baseline agents wrote their own model, and its accuracy against S2
  and against the truth;
- the spread of the tail estimates across the agents' models;
- how often submitted designs miss the limit, per arm;
- the PySim error at which the simulator arm's advantage disappears.

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
  objectives, and its designs miss the limit no more often.
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
