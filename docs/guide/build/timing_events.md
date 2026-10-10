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

## What the numbers have shown

Measured on the examples and the blind tests, on one Windows machine with Vitis 2025.1. These are
single measurements, not benchmarks.

| what | time | where it goes |
| --- | --- | --- |
| a whole-system pysim (two kernels, a crossbar, a host) | 0.1 s | |
| csynth of one small top | 20–60 s | paid again only for a top whose sources changed |
| an XSI run from scratch | 20–30 s | compile RTL 4–5 s, elaborate 11–16 s, compile the testbench 5–6 s, **simulate 0.2–0.3 s** |
| re-running the built XSI testbench | 0.1 s | the simulation, plus loading the snapshot |
| a Vitis cosim run of a small kernel | about 3 min | generate the harness ~30 s, compile ~30 s, **elaborate ~70 s**, simulate ~5 s |

For short simulations, RTL is dominated by fixed costs, not by simulating. That splits a design
search in two:

- **Many workloads on a fixed design** (job lengths, traffic, scenarios) need not be slow at RTL:
  the testbenches read their scenarios from files, so a compiled snapshot can be re-run with new
  vectors for about a tenth of a second. Today this is done by hand; the XSI build step always
  runs the whole runner.
- **Changing the design** (a bus width, a FIFO depth, another kernel) pays csynth of the changed
  tops, then elaboration and compilation: minutes per point. This is where pysim earns its
  place, at seconds per point.

Vitis cosim is slow for a different reason. It builds a general SystemVerilog harness from your C
testbench on every run: transactors for each port, an AXI VIP for the control port, deadlock and
dataflow monitors. It suits one check at the end of a flow, not a loop.

## Concurrent writers

A build, the MCP server and a second build can all append to one log at the same time. Append
mode alone does not make that safe on Windows, which emulates append as a seek to the end
followed by a write: two processes can seek to the same end, and the second overwrites the first.
Each event is therefore written with one unbuffered write of the whole line, under a thread lock
and an OS file lock (`msvcrt.locking` on Windows, `flock` elsewhere). If the lock cannot be had
within ten seconds, the event is dropped rather than the build failed. `tests/test_events.py` has
four processes of four threads each append 2,400 lines at once, and checks every line arrives
whole.

## Environment

| variable | effect |
| --- | --- |
| `WAVEFLOW_EVENTS=off` | record nothing (the test suite sets it) |
| `WAVEFLOW_EVENTS_FILE` | log to this file instead of the project's |
| `WAVEFLOW_EVENT_PARENT` | the span a child process's spans nest under; `waveflow.events.child_env()` sets both for a subprocess |
