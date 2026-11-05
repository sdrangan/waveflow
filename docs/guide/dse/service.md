---
title: The service
parent: Agentic DSE
nav_order: 3
audience: python
summary: "The process that answers every dselib call: it checks the call, synthesizes part by part and charges only parts not built before, schedules synthesis and RTL jobs on k worker slots on a simulated tool clock, enforces the budget and the submit rule, writes small derived artifacts back, and keeps the ledger -- the complete record of what was evaluated and what it cost, which results() returns and score.py reads. Two backends: live (runs the tools; a user's flow, and how the ground truth is measured) and replay (answers from the recorded ground truth; every scored run). Why it runs outside the agent's sandbox."
---

# The service

**Under construction.** The interfaces are in
[`examples/dse_cg/service.py`](../../../examples/dse_cg/service.py); nothing is written yet.

The agent explores through `dselib`, a small Python library in the [skill](skill.md). Every
`dselib` function is a request to the **service**, a separate process that does the work: it runs
(or looks up) the evaluation, charges its cost, and keeps the record. The service is where the
rules of the DSE problem are enforced.

```text
 agent's sandbox                               the service (outside)
 ┌──────────────────────────┐   request      ┌──────────────────────────────────────────┐
 │ agent's Python script    │ ─────────────► │ check ─► cache? ─► charge ─► schedule    │
 │   from dselib import ... │                │                              │           │
 │   synth([...])           │ ◄───────────── │ row ◄── backend ◄────────────┘           │
 │                          │   row          │   live: DseAdaptor ─► the tools          │
 │ results/<eval id>/       │ ◄── files ──── │   replay: the recorded ground truth      │
 └──────────────────────────┘                │ ledger (every call, its cost, its row)   │
                                             └──────────────────────────────────────────┘
```

## Why the adaptor is hidden

The [`DseAdaptor`](context.md) can do everything: simulate, synthesize, run RTL. An agent holding it
could call `synth` directly, and nothing would charge the call, record it or stop it at the budget.
So the agent never holds the adaptor. **Everything routes through `dselib`**, and every `dselib`
call goes to the service, which holds the adaptor and decides what each call costs:

- **The budget is enforced where the agent cannot reach it.** The agent can edit `dselib` itself;
  that changes nothing the service charges.
- **The record is complete.** Every evaluation passes through the service, so the ledger has every
  one, with its cost and its result. The agent's own notes might be incomplete; the ledger is not.
- **The arms are enforced twice.** In the traditional arm `dselib` has no `pysim`, and the service
  also refuses a `pysim` request, so an agent that writes the request by hand still cannot get one.

In an experiment run the service is a separate process and the sandbox has no Waveflow and no
Vivado: the agent can read the design's code but can run it only through `dselib`. A user on their
own machine can also bind `dselib` to a service in the same process; the adaptor is then within
reach, but the service still keeps the record and the cache ([below](#the-service-for-a-user)).

What the agent gets in return is information in a form it can use: small rows ready for a data frame,
derived artifacts instead of raw logs, and every past result through `results()`.

## The `dselib` functions

`dselib` has ten functions. The reference, with an example result for each, is their docstrings in
[`dselib/__init__.py`](../../../examples/dse_cg/skill/dse/dselib/__init__.py). Several are the
adaptor's methods with the service's checks, cache and charges around them:

| function | inputs | returns | behind it | cost |
|---|---|---|---|---|
| `space()` | -- | parameters, constraints, workloads, and which parts each parameter rebuilds | `DseAdaptor.space()` and `rebuilds()` | free |
| `golden(params, workload)` | a design point; a workload name, or `{"name": ..., overrides}` | accuracy metrics, exact | `DseAdaptor.golden()` | per call |
| `pysim(params, workload)` | the same | accuracy, exact; cycles, per-block activity and resources, approximate with a confidence level; an activity table | `DseAdaptor.pysim()`; Waveflow arm only | per call |
| `hls_estimate(points)` | a list of design points | a job id; its rows give each HLS kernel's estimated resources, II and (where computable) latency, with the csynth reports | `DseAdaptor.hls_identities()`, then `DseAdaptor.hls_estimate(kernel, ...)` for each kernel not compiled before; both arms | per new kernel |
| `synth(points)` | a list of design points | a job id; its rows give each point's resources, the sum of its parts, with each part's numbers and a utilization report | `DseAdaptor.identities()`, then `DseAdaptor.synth(node, ...)` for each part not built before | per new part |
| `rtlsim(points, workload)` | a list of design points; a workload | a job id; its rows give measured cycles, the bit-exact check, per-block activity and an activity table | `DseAdaptor.rtlsim()`, after `synth` of any new parts | per new design, then per new workload |
| `wait(job)` | a job id, or a list | the jobs' rows, when done | the service's scheduler | free |
| `results(fn=None)` | a function name or list, optional | every row so far, flattened for a data frame | the ledger | free |
| `budget()` | -- | spent, remaining, by function, jobs running and queued, the tool clock | the ledger | free |
| `submit(params, report)` | the chosen design point; the report with its evidence | accepted, or refused with the reason | the service's submit rule | free |

`params` is always a dict of design parameters, with missing ones taken at their defaults
(`DseAdaptor.fill`). Every evaluation row has a `status`, its `cost_s` and whether it was `cached`;
a failure is a row, never an exception.

## One `dselib` for every design

`dselib` knows nothing about any design. It is a thin client: each function forwards its arguments
to whatever service it is bound to, and the service was built around one adaptor
(`Service(LtbfCgMemAdaptor(), ...)`). The LTBF system and its descriptor-ring variant (V2 in the plan)
use the same `dselib`, bound to different services.

- **It ships with the skill.** It is plain Python with no Waveflow dependency, so it runs in a
  sandbox that has no Waveflow. It lives in the skill's folder and is copied with it.
- **It is bound at run time**, in one of two ways: to a service at an address, through the
  `DSE_SERVICE` environment variable (an experiment run, or tools on another machine), or to a
  service in the same process, with `dselib.bind(service)` (a user on their own machine).
- **The two arms are two renderings of one file.** The traditional arm's copy has the `pysim`
  block removed; nothing else differs, and nothing is written per design.

## What happens on each call

1. **Check.** Unknown parameter names are refused. An illegal design (`DseAdaptor.legal`) comes back
   as a row with `status: "illegal: n > 16"`, at no cost. `pysim` is refused in the traditional
   arm.
2. **Look up.** A result the ledger already holds is returned again, marked `cached`, at no cost.
3. **Charge.** The cost of what is new is taken from the budget. A call the budget cannot cover is
   refused before it runs.
4. **Schedule.** `golden` and `pysim` answer at once. `hls_estimate`, `synth` and `rtlsim` become
   **jobs** on `k`
   worker slots: the call returns a job id, and `wait` returns the rows when the jobs are done.
5. **Answer.** The backend produces the row; the service writes any artifacts, appends the call to
   the ledger, and returns the row.

### Synthesis part by part

A design is synthesized in its parts: the sub-tops and the crossbar of
[`DseAdaptor.architecture()`](context.md). For `synth(points)` the service:

1. asks the adaptor for each point's part **identities** (`identities(params)`, a hash of what each
   part would synthesize; cheap, nothing is built);
2. looks each identity up in the ledger;
3. charges and synthesizes only the parts it does not hold (`DseAdaptor.synth(node, params)` on the
   live backend), one job each;
4. returns, for each point, the sum over its parts and each part's own numbers.

So a design point that differs from an earlier one only in `R` costs only the systolic device's
synthesis. The agent does not manage this; it calls `synth` on whole design points, and the
economics follow.

`rtlsim` simulates the whole DUT, so its identity combines every part's identity with the workload.
A design's first `rtlsim` pays for elaboration and simulation (and the synthesis of any parts not yet
built); another workload on the same design pays for simulation alone.

### The tool clock

Costs are charged in **tool-seconds**: the time the tool takes on the lab machine. The service keeps
a simulated **tool clock**. A job starts when a worker slot is free and ends its duration later, and
`wait` advances the clock to the end of the jobs waited for. The agent's own thinking does not move
the clock.

The clock gives two numbers per run: the tool-seconds spent (the budget) and the elapsed tool time
with `k` jobs in parallel (how long a team with `k` licenses would have waited).

## The ledger

The ledger is the run's complete record: every call, its arguments, its cost, its start and end on
the tool clock, and the row it returned. Synthesis is recorded **per part**: one row per part
identity, shared by every design point that uses that part.

The agent reads it with `results()`:

```python
import pandas as pd
from dselib import results

pysim = pd.DataFrame(results("pysim"))             # one function's rows
measured = pd.DataFrame(results(["synth", "rtlsim"]))
```

The rows are flattened for a data frame: one column per parameter, and dotted columns for nested
values (`lut.est`, `lut.level`, `parts.vec.lut`, `per_block.mm.busy`). Comparing a prediction with a
measurement is one merge on the parameter columns. An agent that loses track of its work, for example
after its conversation is compacted, recovers it exactly with one call.

The agent may keep its own records as well (a fitted model, notes on regions ruled out), but the
ledger is the authority. The agent's final report is checked against it.

**How long a ledger lives:**

| | lifetime | why |
|---|---|---|
| an experiment run | one run; each run starts empty | otherwise a run would inherit earlier runs' measurements for free |
| a user | the project; the ledger file stays | a synthesis done yesterday is still there today, and is not paid for twice |

## Artifacts

Some evaluations produce more than a row. The service writes small, **derived** files into
`results/<eval id>/` in the agent's working directory and puts their paths in the row's `artifacts`:

| function | artifacts |
|---|---|
| `hls_estimate` | each kernel's csynth report |
| `synth` | utilization per instance; timing summary |
| `rtlsim` | per-block activity: one row per block and interval, with the fractions busy, stalled on input, stalled on output |
| `pysim` | the same per-block activity table, approximate |

Derived files rather than raw waveforms: a VCD of a few thousand cycles is megabytes of signal
changes, which an agent would spend most of its context reading. An activity table carries what an
engineer takes from a waveform viewer, at a size a model can read. Each function returns the
artifacts it naturally produces, so neither arm gets a kind of file the other does not.

## Backends

The service answers through one of two backends:

| | answers by | used for |
|---|---|---|
| **live** | calling the adaptor: `golden`, `pysim`, `hls_estimate(kernel, ...)`, `synth(node, ...)` and `rtlsim`, which run the tools; the cost is the time the tools took | a user's flow; measuring the ground truth |
| **replay** | looking up the recorded ground truth by identity: every result and its recorded cost | every scored experiment run |

The ground truth for the CG experiment (the grid called G1 in the plan) is the live backend driven
over a pre-registered grid of design points, with every row, artifact and cost recorded. Replay then
serves those records. A scored run takes minutes instead of days, and every run sees exactly the same
numbers, so the two arms are compared on equal terms.

## Submitting

`submit(params, report)` ends a run. The service accepts it only if the design was synthesized and
RTL-simulated in this run, on the workloads the objective names. A prediction alone is not an answer.

The report lists the evidence for each constraint and the objective, each number with the function
that produced it (`"source": "synth"`). The service records it, and the scoring checks every number
against the ledger: a number never returned, or a pysim prediction reported as a measurement, counts
as an error.

## The service for a user

The same service serves a user in their own project, either in the same process
(`dselib.bind(Service(...))`) or as a server, with three differences:

- the **live** backend, running the tools on the user's machine;
- **no objective** given in advance (the user's prompt has it), so `submit` checks the workloads the
  report names;
- a **budget only if the user sets one**.

A user starts it (`python -m examples.dse_cg.service --live`, not written yet) and sets `DSE_SERVICE`
to its address; the skill's `dselib` finds it there.
