# Plan: a toy experiment on the value of a calibrated simulator

**Status: DRAFT, not started, written 2026-11-14.** Follows the tight-deadline
experiment of `plans/dse_cg.md` and reuses its service, ledger and scoring where
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
| RTL simulation | True cycles and overflow events for the requested length | about 1,000 cycles/s |
| PySim (simulator arm only) | The true model with a tunable error: a timing bias, a spread, one effect left out | about 10⁶ cycles/s |

The tool rates are assumptions taken from the CG grid (about 850k cycles in
12–15 min of RTL simulation, about 1 s in PySim). They are parameters of the
harness.

The harness reuses the service's ledger and scoring if its backend can be
swapped (question 2 in S0). The virtual clock is event-driven over 2 worker
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

### S0. Answers from Ali, before building anything

Four questions decide how much work S1 is and whether the toy's central
assumption holds. Ask them first and write the answers here.

**Q1. Which parts of the toy does Waveflow already model?** The true model
needs a FIFO, a banked shared memory, an arbiter and a traffic generator. The
CG system has a shared memory; the others may not exist.

- *If Waveflow has them:* build the true model from them. S1 is shorter. The
  true model and PySim then share code, so PySim's error must be added on
  purpose (§3), and the hand-written simulator in S2 must not reuse those
  units.
- *If it doesn't:* write the true model as a small standalone event-driven
  Python program. It doesn't need to be Waveflow. S1 takes longer.

**Q2. Can the service answer calls from a model instead of the measured
grid?** In the DAC runs, the service looks each call up in the 384-design table,
charges the measured tool time to the ledger and scores the run.

- *If the lookup is one replaceable piece:* reuse the service, the ledger, the
  scoring and the agent-run scripts. S1 becomes the true model plus a small
  adapter.
- *If it isn't:* write a minimal standalone harness (tool calls, virtual clock
  over 2 worker slots, ledger), and keep its ledger in the same format so the
  existing scoring and plotting scripts still work. Don't refactor the service
  for this.

**Q3. How fast are RTL simulation and PySim, in cycles per second, for a design
of this size?** The toy works only if checking the tail with RTL is too slow for
the baseline to test many designs. At ε = 10⁻⁴ a tail estimate needs about
3×10⁷ cycles.

- *If RTL runs at about 10³ cycles/s and PySim is 100× faster or more:* keep
  the harness defaults (§3). One RTL tail estimate takes about 8 h, the whole
  budget.
- *If RTL is much faster, say 10⁴ cycles/s:* one RTL tail estimate takes about
  50 min, and the baseline can test about 10 designs in 8 h. Make the
  requirement stricter (smaller ε) or the blocks longer, so that one RTL tail
  estimate still takes at least a quarter of the budget.
- *If PySim is less than about 30× faster than RTL:* speed alone won't separate
  the arms. Say so in the S6 note, and decide with the user whether to go on.
- *If nobody knows yet:* run the harness at two speed ratios, 100× and 1,000×,
  and report both.

**Q4. Is the DAC-period work merged into Waveflow's `main`?** The user asked Ali
to merge it there. Start S1 from `main` once the merge is done. If it isn't done
yet, start from `paper/ltbf_cg_dse_agentic` and rebase onto `main` later. This
doesn't change the plan.

**Done when:** the four answers and the choice made for each are written in
this step.

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
