## The frame: `stream_inband`

You are building in the `stream_inband` frame: a host-launched streaming
accelerator on the in-band command-response contract. `frame.md` in this
directory is the **specification** the design must meet -- protocol, errors,
the comparisons, the report. Read this first, then `frame.md`, then the
function spec you were given.

The design is **hook-first**. Waveflow generates the mechanical parts -- the
schemas' C++ headers and serializers, and the kernel's boundary (prototype,
interface pragmas, register map). You write the design: the function, the
scenarios, the kernel body in C++, a Python model, and an ordinary C++
testbench. If you started from `waveflow new-accel`, every one of those files
already exists, renamed from the reference design, with the arithmetic
stubbed to an identity; it builds and passes as generated.

## The reference design

**Your reference design is `stream_inband`.** Read it before writing
anything:

```
waveflow_get_example("stream_inband")                          # the card: modules, ports, hooks
waveflow_get_example("stream_inband", file="poly.py")          # schemas, the model, the module
waveflow_get_example("stream_inband", file="scenarios.py")     # intents, expected responses, checker
waveflow_get_example("stream_inband", file="poly_body_impl.tpp")  # the kernel body
waveflow_get_example("stream_inband", file="poly_tb.cpp")      # the C++ testbench
waveflow_get_example("stream_inband", file="poly_build.py")    # the BuildDag
waveflow_get_doc("docs/examples/stream_inband/index.md")       # the contract: rules 1-7
waveflow_get_doc("docs/examples/stream_inband/decisions.md")   # what your spec must decide
waveflow_get_doc("docs/examples/stream_inband/why_not.md")     # the designs not to build
```

**The contract is not negotiable.**  Configuration travels in the command
header, never over AXI-Lite; on an error the kernel closes its output burst
and returns, with no drain and no footer.  If you find yourself writing
configuration registers or a recovery path, reread `why_not.md`.

---

## Stage 1: the specification

`frame.md` §F5 says exactly what each piece must contain.

1. **The schemas**, in `<name>.py`: command header (carrying the function's
   parameters), response header, footer (only if the spec has per-command
   results), error enum.  The register map holds only the status.
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
   schema; that is where hand-packing bugs enter.  Below it, one line for each
   item of `decisions.md`: how your design answers it.

Then:

- run the worked examples and show the output;
- show the checker **rejecting** at least two wrong outputs you construct.
  Name each and give the checker's output.

**Stop here.** Summarize Stage 1 and end your turn -- even in a
non-interactive session; the approval is the next message. Do not write the
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
   its ports and a status-only `VitisRegMap` (`halted`, `error`, `tx_id`). Its Python `body()` is a thin port wrapper
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

## Rules for this frame

- **Scenarios are pre-loaded**, because Vitis csim requires it: the testbench
  plays a scenario's whole stimulus, then drains the outputs. No scenario may
  make an input depend on an earlier output. A design that needs that wants a
  `FreeRunMod` with the concurrent BFM testbench, which is a different frame
  (`waveflow_list_frames()`).
- A design in this frame is accepted only when every comparison of §F7
  passes.
