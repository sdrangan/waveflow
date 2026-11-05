---
title: The DSE problem
parent: Agentic DSE
nav_order: 1
audience: python
summary: "The parametric DSE problem stated precisely: design parameters and workload parameters, legality constraints, metrics, an objective with constraints, evaluations that differ in cost and in what they can see (golden, pysim, synth, rtlsim), a budget of tool time, and the rule that the answer must be measured. Then how it is written down for an agent, in three layers: a skill (the method), a design context file DSE.md (this design), and a prompt (this task). The LTBF system of examples/ltbf_cg_mem_dse is the example throughout."
---

# The DSE problem

**Under construction.** The pieces named here are in the [`examples/dse_cg`](../../../examples/dse_cg/README.md)
skeleton; most are not written yet.

## The parts of a problem

### Parameters

A **design parameter** changes the hardware: a new value is a new design, which must be synthesized
again. A **workload parameter** changes what the hardware is asked to do: a new value costs only
another simulation. The distinction matters because it decides what an evaluation costs.

For the LTBF system (the rest of its build is fixed):

| parameter | kind | meaning |
|---|---|---|
| `n` | design | antenna array side: N_rx = n × n receive antennas |
| `R`, `C` | design | systolic array rows and columns |
| `W` | design | operand width of the fixed-point formats, bits |
| `L` | design | the vector unit's lanes |
| `t` | workload | CG iterations of the long-term update |
| `form` | workload | how the CG step applies Q̂: factored, or Q̂ formed once (explicit) |
| `target` | workload | the UEs' SINR targets the accuracy is measured at |

The **design space** is every combination of the design parameters' values that is legal.

### Constraints on legality

Some combinations are not a design at all. For the LTBF system, the systolic array must be one of the
shapes its engine is built at (`(R, C) in [(2, 4), (4, 4), (4, 8), (8, 8)]`), and the antenna array
must leave Shared Mem on chip (`n <= 16`: at 32 × 32 its block RAM alone is 3.7 devices). These rules
are part of the problem, not something to discover by failed builds.

### Workloads

A **workload** fixes the workload parameters and the input data for one kind of measurement. The LTBF
system has three: `long_term`, the long-term updates of the three sectors back to back, to measure
T_LT; `slot`, one slot of 14 symbols at the radio's pace after the update, to see whether the system
keeps up; and `accuracy`, held-out channel drops at each set of SINR targets, to measure accuracy. A
metric is only meaningful on the workload it was measured with.

### Metrics

What can be measured about a design on a workload:

| metric | what | units |
|---|---|---|
| `median_loss_db`, `p90_loss_db` | SINR loss against exact floating-point LTBF, median and 90th percentile over the UEs | dB |
| `t_lt_ms` | the long-term update of the three sectors, back to back | ms |
| `dropped` | symbols the Ingest DMA dropped in a slot (0: it keeps the radio's pace) | count |
| `lut`, `ff`, `dsp`, `bram`, `uram` | resources | counts |
| per-block activity | how much of the time each block is busy, or stalled waiting for input or for room to send output | fractions |

### The objective

An **objective** picks one metric to minimize (or maximize) and puts limits on others. For example:

> Minimize LUTs, subject to a median loss ≤ 0.1 dB at SINR targets [−6, 3] dB and T_LT ≤ 10 ms.

The answer is a set of design parameters. It is good if it meets the limits and its LUT count is
close to the best that any legal design reaches.

### Evaluations, and what they cost

Each kind of evaluation sees a different part of the answer, at a different price:

| evaluation | accuracy | cycles | resources | cost (order of) |
|---|---|---|---|---|
| `golden`: untimed bit-exact simulation | exact | — | — | a second |
| `pysim`: bit-exact, calibrated timing and resource models | exact | approximate | approximate | seconds |
| `hls_estimate`: the HLS compiler's report, per kernel | — | estimated, where computable, HLS kernels only | estimated, HLS kernels only | about a minute a kernel |
| `synth`: synthesis | — | — | measured | minutes to hours a design |
| `rtlsim`: RTL simulation | (checks bit-exact) | measured | — | minutes a design |

An **estimated** number is the HLS compiler's own: Vitis HLS reports resources and, when it can
compute one, latency for each kernel before Vivado synthesis. It is rough (LUTs especially), and it
says nothing about the logic around the kernels: the adaptors, memory windows and crossbar are
hand-written RTL or vendor IP, not HLS. A hardware team uses it as the quick look before
synthesizing.

An approximate number comes with a **confidence level**, which says whether the model was calibrated
around that point (`exact`, `interpolated`), outside it (`extrapolated`), or not at all
(`uncalibrated`).

### The budget and the answer

The explorer has a **budget of tool time**: the total time its evaluations take on the tools. The
explorer's own thinking is not counted. The budget is too small to measure every design, so an
explorer has to choose which designs to synthesize.

The answer must be **measured**: the submitted design must have been synthesized and RTL-simulated.
A prediction alone is not an answer.

The traditional flow has `golden`, `hls_estimate`, `synth` and `rtlsim`; Waveflow adds `pysim`.
Everything else about the problem is the same. `hls_estimate` is in both so that the traditional flow
has the quick look a real hardware team has: pysim has to beat the HLS compiler's estimates, not
only full synthesis. It should, because the compiler sees each kernel alone, while pysim sees the
whole design, the logic around the kernels, and how the parts hold each other up.

## How the problem is given to an agent

The agent gets the problem in three layers, split by how often each changes:

| layer | what | written by | file |
|---|---|---|---|
| **skill** | how to explore *any* design: the evaluation functions, the rules, the report | Waveflow, once | [`skill/dse/SKILL.md`](../../../examples/dse_cg/skill/dse/SKILL.md) |
| **context** | *this* design: system, modules, parameters, constraints, workloads, metrics, costs | generated from the design | `DSE.md`, from [`DSE.md.tmpl`](../../../examples/dse_cg/DSE.md.tmpl) |
| **prompt** | *this* task: the objective | the user | typed |

The agent then works through a small Python library, `dselib`, whose functions are the evaluations
above. Every call goes to a **service** that runs the tools, charges the time to a ledger and
refuses calls once the budget is spent:

```python
from dselib import space, golden, pysim, hls_estimate, synth, rtlsim, wait, results, budget, submit

space()                                         # the parameters, constraints and workloads, as data
golden({"W": 12}, "accuracy")                   # exact accuracy, no timing
pysim({"R": 2, "C": 4, "W": 12}, "long_term")   # exact accuracy, approximate T_LT and resources
job = synth([{"R": 2, "C": 4, "W": 12}])        # measured resources, as a job
wait(job)                                   # the measured rows
submit(params, report)                      # the answer, with the evidence for it
```

The library's reference is its docstrings,
[`dselib/__init__.py`](../../../examples/dse_cg/skill/dse/dselib/__init__.py).

### Where each part of the problem lives

| part | layer |
|---|---|
| parameters, kinds, values, defaults | context (generated from the design) |
| legality constraints | context |
| workloads | context |
| metrics | context |
| costs of each evaluation | context |
| the evaluation functions and their rules | skill |
| what the answer must include | skill |
| the objective | prompt |
| the budget | service (`budget()` reports it) |

The context is **generated** from the design's Python, so it cannot drift from the hardware: the
parameter table comes from the design's parameter space, and the list of what each parameter forces
to be rebuilt comes from comparing the builds at two values.

[Skills](skill.md) explains how an agent gets the skill and reads it.
