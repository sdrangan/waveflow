---
name: dse
description: Explore a hardware design's parameter space to meet an objective (for example, the fewest LUTs at a given accuracy and cycle budget) with simulation, synthesis and RTL simulation, within a budget of tool time. Use when asked to choose, size or optimize a design's parameters.
argument-hint: <path to the design's DSE.md> <objective>
---

<!--
  Kept in the Waveflow arm and in the shipped skill; removed in the traditional arm: the blocks
  between the arm:waveflow and /arm markers.  brief.py does the removal (and drops this comment).
-->

# Design-space exploration

Your task: **$ARGUMENTS**

The first argument is the path to the design's `DSE.md`; the rest is the objective. Read `DSE.md`
first. It describes the system, its parameters and their legal values, its workloads, and what each
evaluation costs. Its links lead to the design's documentation and source, if you want to read them.

## The library

The library is `dselib`, in this skill's directory. Every function is a call to a DSE service,
which runs the tools, charges each evaluation to a ledger and enforces the budget:

```python
import sys; sys.path.insert(0, "<this skill's directory>")
from dselib import space, golden, hls_estimate, synth, rtlsim, wait, results, budget, submit
<!-- arm:waveflow -->
from dselib import pysim
<!-- /arm -->
```

`help(dselib.<function>)` gives the full reference, with an example result for each function.

## What each evaluation gives you

| function | gives you |
|---|---|
| `golden(params, workload)` | accuracy, **exact** (bit-exact, untimed). No cycles, no resources. |
<!-- arm:waveflow -->
| `pysim(params, workload)` | accuracy, **exact**; cycles, per-block activity and resources, **approximate**, each with a confidence level. |
<!-- /arm -->
| `hls_estimate(points)` | resources and, where computable, latency of each HLS kernel, **estimated** by the HLS compiler. Not the logic around the kernels. |
| `synth(points)` | resources and II, **measured**. |
| `rtlsim(points, workload)` | cycles and per-block activity, **measured**. |

`DSE.md` gives the cost of each one for this design. `hls_estimate`, `synth` and `rtlsim` run as
jobs, a few at once;
`wait` collects their results. The budget counts tool time on the service's clock, not your thinking.

## Rules

- Every call is charged. `budget()` shows what you have spent and what is left; the run ends when
  the budget is spent.
- A failure is a result, not an exception: a row with a `status` and the time it cost.
- `results()` returns everything evaluated so far, so you never need to repeat a call.
- The design you submit must be one you have synthesized **and** RTL-simulated, on the workloads the
  objective names.
- Work in Python. Write scripts and notes in the working directory as you like.

## Hints

- Use cheap evaluations before expensive ones.
- Verify a design before you submit it.

## Your report

End by calling `submit(params, report)`, with `report` a dict:

```python
{
    "params": {...},                  # the design submitted, as passed to synth
    "evidence": [                     # one entry per constraint and the objective
        {"metric": "lut", "value": 18432, "source": "synth"},
        {"metric": "cycles_per_solve", "value": 3870, "workload": "steady", "source": "rtlsim"},
        {"metric": "mse_db", "value": -21.4, "workload": "snr_sweep", "source": "golden"},
    ],
    "rationale": "a few sentences: how you searched, and why this design",
}
```

`source` names the function that produced the number. The report is checked against the ledger.
