---
title: The DSE problem
parent: Agentic DSE
nav_order: 1
audience: python
summary: "The parametric DSE problem stated precisely: design parameters and workload parameters, legality constraints, metrics, an objective with constraints, evaluations that differ in cost and in what they can see (golden, pysim, synth, rtlsim), a budget of tool time, and the rule that the answer must be measured. Then how it is written down for an agent, in three layers: a skill (the method), a design context file DSE.md (this design), and a prompt (this task). The CG detector is the example throughout."
---

# The DSE problem

**Under construction.** The pieces named here are in the [`examples/dse_cg`](../../../examples/dse_cg/README.md)
skeleton; most are not written yet.

## The parts of a problem

### Parameters

A **design parameter** changes the hardware: a new value is a new design, which must be synthesized
again. A **workload parameter** changes what the hardware is asked to do: a new value costs only
another simulation. The distinction matters because it decides what an evaluation costs.

For the CG detector:

| parameter | kind | meaning |
|---|---|---|
| `K` | design | users solved for: the CG system is K × K |
| `L` | design | lanes: complex elements per bus word |
| `R`, `C` | design | systolic array rows and columns |
| `cmul` | design | real multiplies per complex multiply (3 or 4) |
| `W` | design | word width of the fixed-point formats, bits |
| `g_s` | design | guard bits on the CG scalars |
| `mem_dw` | design | memory bus width, bits |
| `sob_depth`, `cmd_depth` | design | channel and command-queue depths |
| `nit` | workload | CG iterations per solve (at most `K`) |
| `snr_db` | workload | the channel SNR the accuracy is measured at |

The **design space** is every combination of the design parameters' values that is legal.

### Constraints on legality

Some combinations are not a design at all. For the CG detector, the array's rows must tile `K`
(`K % R == 0`), the lanes must tile the columns (`C % L == 0`), and the array must fit the device
(`R * C <= 256`). These rules are part of the problem, not something to discover by failed builds.

### Workloads

A **workload** fixes the workload parameters and the input data for one kind of measurement. The CG
detector has two: `steady`, a stream of solves to measure throughput, and `snr_sweep`, a set of
channels at several SNRs to measure accuracy. A metric is only meaningful on the workload it was
measured with.

### Metrics

What can be measured about a design on a workload:

| metric | what | units |
|---|---|---|
| `mse_db`, `ber` | accuracy against the floating-point answer | dB, fraction |
| `cycles_per_solve` | throughput | cycles |
| `lut`, `ff`, `dsp`, `bram` | resources | counts |
| per-block activity | how much of the time each block is busy, or stalled waiting for input or for room to send output | fractions |

### The objective

An **objective** picks one metric to minimize (or maximize) and puts limits on others. For example:

> Minimize LUTs, subject to MSE ≤ −20 dB at SNR 10 dB and ≤ 4,000 cycles per solve.

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

space()                                     # the parameters, constraints and workloads, as data
golden({"K": 8, "W": 12}, "snr_sweep")      # exact accuracy, no timing
pysim({"K": 8, "W": 12, "R": 4}, "steady")  # exact accuracy, approximate cycles and resources
job = synth([{"K": 8, "W": 12, "R": 4}])    # measured resources, as a job
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
