---
title: Timing Events
parent: Build System
nav_order: 8.5
summary: "Where a build's wall clock goes. Every build step, nested step, toolchain run (vitis-run, vivado, each XSI phase) and MCP tool call is a timing span, nested the way the work is, appended to the project's .waveflow/events.jsonl and handed back to the caller. run_dag_cli --timing prints one build's tree; waveflow.events.analyze_events breaks a project's history down by category (synth, RTL sim, pysim) and gives per-step statistics -- what one pysim costs against one RTL simulation, for a person or an agent deciding how often each can be afforded."
---

# Timing Events

A design-space search is a loop of evaluations, and what it can afford depends on what each kind
of evaluation costs. A pysim run takes about a second. An RTL run takes tens of seconds even when
the simulation itself takes a fraction of one, because compiling and elaborating dominate. Waveflow
records these costs as the work happens, so they can be read from a project's own history instead
of being guessed.

## What is recorded

Every unit of work is a **span**: a name, a kind, a start, an elapsed time, an outcome and a parent.

| kind | what | recorded by |
| --- | --- | --- |
| `build` | one build command | `run_dag_cli` |
| `step` | a build step, run or up to date | `BuildDag.run` |
| `tool` | a toolchain run: `vitis-run`, `vivado`, `xsi` | the toolchain runners |
| `phase` | a phase of an XSI run: `compile_rtl`, `elaborate`, `compile_tb`, `simulate` | the XSI runner's `WF_PHASE` lines |
| `mcp` | an MCP tool call | the MCP server |

Spans nest the way the work does. The parent of a new span is whatever span is open when it
starts, so a `BuildDag` run from inside another DAG's step lands under that step. The csynth
and system-XSI steps do exactly that. A `vitis-run` lands under the step that launched it,
whether that step is csim, csynth or cosim.

Each span is appended as one JSON line to the project's log, `<project>/.waveflow/events.jsonl`,
where the project is the build's `root_dir`. An MCP call outside any build logs to the server's
working directory. Writers are serialized by a file lock, so builds and the MCP server can share
a log safely. Set `WAVEFLOW_EVENTS=off` to record nothing; the test suite does this.

## One build: `--timing`

A two-kernel bus system, rebuilt through its RTL gate with synthesis already up to date:

```text
$ python scale_sum_build.py --through compare --synth check --timing
...
timing:
    scale_sum_build.py                   28.5 s
      codegen                            0.0 s
        ...
      scenario                           0.0 s
      csynth                             up to date
      pysim                              0.1 s
      system_rtl                         0.0 s
        xbar_ip                          0.0 s
        system_top                       0.0 s
      system_xsi                         28.0 s
        scenario                         up to date
        rtl                              up to date
        harness                          0.0 s
        xsi_run                          28.0 s
          xsi                            27.7 s
            compile_rtl                  5.0 s
            elaborate                   16.0 s
            compile_tb                   6.4 s
            simulate                     0.3 s
      compare                            0.1 s
```

The pysim of the whole system took 0.1 s. The RTL simulation of the same scenario took 28 s, of
which 0.3 s was simulating: the rest was compiling and elaborating. A Vitis cosim run has the
same shape with a much larger fixed cost, about three minutes for a small kernel, most of it
elaborating its SystemVerilog testbench.

`run_dag_cli` also returns the spans to its caller, and `waveflow.events.collect()` hands back the
spans of any block of work, so a sweep that calls `dag.run(...)` in Python has the same numbers.

## A project's history: `analyze_events`

```python
from waveflow.events import analyze_events, format_analysis

a = analyze_events("path/to/project")
print(format_analysis(a))
```

It reports:
- **Seconds per category** (synth, RTL sim, pysim, other build, MCP), each second charged once,
  to the most specific category running then. A step and the toolchain run inside it are not
  counted twice.
- **Per-name statistics:** how many times each step or tool ran, and its mean, minimum and
  maximum time. This is the cost of one evaluation at each fidelity.

The [blind-test summary](../ai_tooling/blind.md) uses the same events, together with the agent's
transcript, to report where a run's wall clock went: synthesis, RTL simulation, pysim, other
tools, and the agent's own reasoning.
