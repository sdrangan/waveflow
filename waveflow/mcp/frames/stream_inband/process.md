# Building a streaming in-band accelerator with Waveflow

You are building a hardware accelerator with Waveflow, in the `stream_inband`
frame. This file is the process: what to do, in what order, and which tool to
use at each step. `frame.md` in this directory is the **specification** the
design must meet — protocol, errors, the three comparisons, the report. Read
this first, then `frame.md`, then the function spec you were given.

The work has two stages, and the boundary between them is the point of the
exercise:

- **Stage 1 writes the spec** — schemas, an independent oracle, the scenarios
  and the checker. Then you **stop** for review.
- **Stage 2 builds the accelerator** against that frozen spec.

An accelerator that agrees with a model you wrote at the same time proves
nothing. Stage 1 exists so that Stage 2 has something to be wrong against.

---

## Before you start: learn the machinery

Waveflow is probably not in your training data. Do not guess at its API —
every name below is one call away.

| To find out | Call |
| --- | --- |
| what the docs cover | `waveflow_browse()`, then `waveflow_browse("guide")` |
| how Waveflow does *X* | `waveflow_search("X")` — best with Waveflow's own words |
| what Waveflow calls *X* | `waveflow_browse(section)` and read the summaries |
| who uses a name, and how | `waveflow_find_usage("HostActivated")` |
| which example to copy | `waveflow_list_examples()` |
| a whole file from one | `waveflow_get_example("stream_inband", file="poly.py")` |
| a whole doc page | `waveflow_get_doc(path)` |

All six are also `waveflow kb <cmd>` on the command line.

**Your reference design is `stream_inband`.** Read it before writing
anything:

```
waveflow_get_example("stream_inband")                        # the card: modules, ports, hooks
waveflow_get_example("stream_inband", file="poly.py")        # schemas, HwModule, testbenches
waveflow_get_example("stream_inband", file="poly_evaluate_impl.tpp")   # the hand-written hook
waveflow_get_example("stream_inband", file="poly_build.py")  # the BuildDag
waveflow_get_doc("docs/examples/stream_inband/index.md")     # the tutorial
```

Examples that `waveflow_list_examples()` does not return are **not** models to
copy, whatever else is in the tree.

Anything a tool tags as generated — the kernel C++, the testbench C++, every
header under `include/` — you may read and must never edit.

---

## Stage 1: the specification

Write these, and nothing else, into `spec/`. `frame.md` §F5 gives the exact
file list and what each must contain.

1. **`spec/<name>_schemas.py`** — every schema: the command header, the
   response header, the footer, the error enum, the register-map parameter
   types.
   - `waveflow_search("DataList schema definition")`,
     `waveflow_search("EnumField generated C++ enum header")`
   - `waveflow_find_usage("DataList")` for the real declarations
   - validate each with `waveflow_validate_schema` before moving on
2. **`spec/layout.md`** — the word-by-word layout of every header, footer and
   burst, obtained by **serializing an instance with Waveflow** and writing
   down what came out. Do not derive it by reasoning about the schema; that
   is the step where hand-packing bugs enter.
3. **`spec/oracle.py`** — plain numpy. It must **not** import the accelerator
   module, `HwModule` or SimPy. It implements the function, the F3/F4 framing
   and errors, and the register-map status a run should end with.
4. **`spec/scenarios.py`** — every scenario built from fixed seeds into
   `spec/vectors/<scenario>/`: the parameters, the whole input stream
   including malformed framing, and the oracle's expected output and status.
5. **`spec/check.py`** — `check.py <results_dir>`, one PASS/FAIL line per
   criterion, nonzero exit on any failure.

Then:

- run `oracle.py` on the function spec's worked examples and show the output;
- show `check.py` **rejecting** at least two wrong outputs you construct.
  Name each mutant and give its output.

**Stop here.** Summarize Stage 1 and wait for approval. Do not write the
accelerator yet.

---

## Stage 2: the accelerator

`spec/` is now frozen. **Do not modify anything under it.** If you believe
the spec, the oracle or a scenario is wrong, stop and explain why — do not
edit around it, and do not relax an acceptance criterion. If you cannot meet
one, report the best value you reached and what limits it.

1. **`<name>.py`** — the `HostActivated` module with its `VitisRegMap` and
   `on_start` persistent loop, the pysim testbench, and the `SeqTB`. Import
   the schemas from `spec/`; do not redefine them.
   - `waveflow_search("HostActivated host launched module")`
   - `waveflow_search("register map parameter array")`
   - `waveflow_search("persistent loop END command")`
   - `waveflow_find_usage("SeqTB")`
2. **`<name>_<method>_impl.tpp`** — the hand-written compute hook, the only
   C++ you write.
   - `waveflow_search("hand-written compute hook")`
   - `waveflow_search("error when TLAST arrives early")` for the framing checks
   - `waveflow_get_example("stream_inband", file="poly_evaluate_impl.tpp")`
3. **`<name>_build.py`** — a `BuildDag` driven by `run_dag_cli`:
   `build_inputs` → `py_sim` → `gen_include` → `gen_kernel` → `gen_tb` →
   `csim` → `validate_csim` → `csynth` → `inspect_synth` → `cosim` → the
   timing steps, plus a step that runs `spec/check.py` against the pysim,
   csim and cosim results.
   - `waveflow_search("BuildDag build steps run_dag_cli")`
   - `waveflow_get_example("stream_inband", file="poly_build.py")`
4. **Calibrate the timing model.** Set `proc_latency` and `proc_ii` from the
   measured RTL, not from a guess.
   - `waveflow_search("pysim vs cosim timing tolerance")`
5. **`results/report.md`** — the criteria table of `frame.md` §F8.

---

## The rules

- **Never hand-pack words.** Every header, footer and sample burst goes
  through its Waveflow schema or the Waveflow array utilities, in Python and
  in C++ alike.
- **Never edit a generated file.** If the machinery cannot express something
  you need, **stop and report it** with the exact error you saw. Working
  around it by hand-writing generated output is the one failure that makes
  the whole flow worthless.
- **Never write your own PASS column.** A criterion passes when `check.py`
  or a build step says so.
- **Scenarios are pre-loaded**, because Vitis csim requires it: the testbench
  pushes every command and burst of a scenario before the kernel runs, then
  drains the outputs. No scenario may make an input depend on an earlier
  output. A design that needs that wants a `FreeRunMod` with the concurrent
  BFM testbench, which is a different frame.
- A design is accepted only when **all three comparisons** of §F7 pass:
  oracle vs pysim, pysim vs csim/cosim, and pysim timing vs cosim cycles.
  When one fails, say **which layer** failed before changing anything.
