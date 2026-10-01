# Building a streaming in-band accelerator with Waveflow

You are building a hardware accelerator with Waveflow, in the `stream_inband`
frame. This file is the process: what to do, in what order, and which tool to
use at each step. `frame.md` in this directory is the **specification** the
design must meet -- protocol, errors, the comparisons, the report. Read this
first, then `frame.md`, then the function spec you were given.

The design is **hook-first**. Waveflow generates the mechanical parts -- the
schemas' C++ headers and serializers, and the kernel's boundary (prototype,
interface pragmas, register map). You write the design: the function, the
scenarios, the kernel body in C++, a Python model, and an ordinary C++
testbench. If you started from `waveflow new-accel`, every one of those files
already exists, renamed from the reference design, with the arithmetic
stubbed to an identity; it builds and passes as generated.

The work has two stages, and the boundary between them is the point:

- **Stage 1 writes down what the accelerator must do** -- schemas, the
  function, the scenarios and their expected responses. Then you **stop** for
  review.
- **Stage 2 builds it** against that frozen specification.

An accelerator that agrees with a model you wrote at the same time proves
nothing. Stage 1 exists so that Stage 2 has something to be wrong against.

---

## Before you start: learn the machinery

Waveflow is probably not in your training data. Do not guess at its API --
every name below is one call away.

| To find out | Call |
| --- | --- |
| what the docs cover | `waveflow_browse()`, then `waveflow_browse("guide")` |
| how Waveflow does *X* | `waveflow_search("X")` -- best with Waveflow's own words |
| what Waveflow calls *X* | `waveflow_browse(section)` and read the summaries |
| who uses a name, and how | `waveflow_find_usage("cpp_body")` |
| which example to copy | `waveflow_list_examples()` |
| a whole file from one | `waveflow_get_example("stream_inband", file="poly.py")` |
| a whole doc page | `waveflow_get_doc(path)` |

All of these are also `waveflow kb <cmd>` on the command line.

**Your reference design is `stream_inband`.** Read it before writing
anything:

```
waveflow_get_example("stream_inband")                          # the card: modules, ports, hooks
waveflow_get_example("stream_inband", file="poly.py")          # schemas, the model, the module
waveflow_get_example("stream_inband", file="scenarios.py")     # intents, expected responses, checker
waveflow_get_example("stream_inband", file="poly_body_impl.tpp")  # the kernel body
waveflow_get_example("stream_inband", file="poly_tb.cpp")      # the C++ testbench
waveflow_get_example("stream_inband", file="poly_build.py")    # the BuildDag
waveflow_get_doc("docs/examples/stream_inband/index.md")       # the tutorial
```

Examples that `waveflow_list_examples()` does not return are **not** models to
copy, whatever else is in the tree.

Anything a tool tags as generated -- `gen/`, every header under `include/` --
you may read and must never edit.

---

## Stage 1: the specification

`frame.md` §F5 says exactly what each piece must contain.

1. **The schemas**, in `<name>.py`: command header, response header, footer,
   error enum, register-map parameter types.
   - `waveflow_search("DataList schema definition")`,
     `waveflow_find_usage("DataList")` for real declarations
   - validate each with `waveflow_validate_schema` before moving on
2. **The function, `<name>_eval`**, in `<name>.py`: the spec's arithmetic as a
   pure function, in the operation order and precision the C++ will use.
   Pin it down with the spec's **worked examples** (hand-computed values) in a
   test -- everything downstream trusts this function.
3. **`scenarios.py`**: each scenario as **intents** (from fixed seeds),
   including malformed ones; its stimulus; and its expected response and
   status **computed from the intent**, not by running a protocol model. Keep
   the checker: words, burst boundaries, TLAST flags and status, exactly.
   - `waveflow_get_example("stream_inband", file="scenarios.py")`
   - a missing TLAST is a `StreamBurst(words, tlast=False)` in the stimulus
4. **`layout.md`**: the word layout of every header, footer and burst, from
   **serializing instances with Waveflow** -- not from reasoning about the
   schema; that is where hand-packing bugs enter.

Then:

- run the worked examples and show the output;
- show the checker **rejecting** at least two wrong outputs you construct.
  Name each and give the checker's output.

**Stop here.** Summarize Stage 1 and wait for approval. Do not write the
accelerator yet.

---

## Stage 2: the accelerator

The Stage 1 artifacts are now frozen. **Do not modify them.** If you believe
one is wrong, stop and explain why -- do not edit around it, and do not relax
an acceptance criterion. If you cannot meet one, report the best value you
reached and what limits it.

1. **`<name>_stream_model`**, in `<name>.py`: the whole kernel as a pure
   function of its input stream -- headers, the TLAST rules, the errors --
   calling `<name>_eval` for the arithmetic.
   - `waveflow_get_example("stream_inband", file="poly.py")` (`poly_stream_model`)
2. **The module**: a body-only `HostActivated` (`cpp_body = "body"`) declaring
   its ports and `VitisRegMap`. Its Python `body()` is a thin port wrapper
   around `<name>_eval`, with the timing model (`proc_latency`, `proc_ii`).
   - `waveflow_search("body-only kernel cpp_body")`, `waveflow_find_usage("cpp_body")`
   - **More than one word width?** Do not write a second module: add
     `param_supports = {"bw64": {"in_bw": 64, "out_bw": 64}}` and every width
     gets its own generated top (`<name>_bw64`) calling the same templated
     body. `waveflow_search("param_supports width variant")`
3. **`<name>_body_impl.tpp`**: the whole kernel body in C++ -- the command
   loop, framing, compute and status (`halted = 1;` etc., through the
   reference arguments). Keep its `#pragma HLS INLINE`. Write each
   floating-point multiply and add as a separate statement so the compiler
   cannot fuse them, or the C++ stops matching `<name>_eval` bit for bit.
   - `waveflow_get_example("stream_inband", file="poly_body_impl.tpp")`
   - `waveflow_search("error when TLAST arrives early")`
4. **`<name>_tb.cpp`**: plays each scenario's stimulus with
   `wf::play_stream`, runs the kernel, records with `wf::record_stream`, and
   writes the status. Ordinary C++: loop over scenarios as you need.
5. **`<name>_build.py`**: run it through `summary`, which runs every check:
   `python <name>_build.py --through summary`. (`--through` runs only a
   target's ancestors, so an earlier target can skip checks.)
6. **Calibrate the timing model.** Set `proc_latency` and `proc_ii` from the
   measured RTL, not from a guess.
   - `waveflow_search("pysim vs cosim timing tolerance")`
7. **`results/report.md`**: the criteria table of `frame.md` §F8.

---

## The rules

- **Never hand-pack words.** Every header, footer and sample burst goes
  through its Waveflow schema or the Waveflow array utilities, in Python and
  in C++ alike.
- **Never edit a generated file.** If the machinery cannot express something
  you need, **stop and report it** with the exact error you saw.
- **Never write your own PASS column.** A criterion passes when a check step
  or the build says so.
- **Scenarios are pre-loaded**, because Vitis csim requires it: the testbench
  plays a scenario's whole stimulus, then drains the outputs. No scenario may
  make an input depend on an earlier output. A design that needs that wants a
  `FreeRunMod` with the concurrent BFM testbench, which is a different frame.
- **Keep each Vitis project one directory deep.** Vitis HLS 2025.1 records a
  design file's path relative to a one-level project, so
  `open_project vitis/w32` silently drops the kernel from csim and the link
  fails with `undefined symbol: <kernel>(...)` -- a Vitis defect, not your
  code. To group projects, `cd` into the folder first, then
  `open_project w32`, and give `add_files` absolute paths.
- A design is accepted only when **every comparison** of §F7 passes. When one
  fails, say **which layer** failed -- the function, the protocol model, the
  module, the C++ body -- before changing anything.
