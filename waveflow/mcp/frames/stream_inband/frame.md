# Streaming accelerator frame (read this before the function spec)

This file is the part of every accelerator spec that stays the same from one
design to the next: the protocol, the error behavior, the build flow and the
process. Each function spec (`01_gain_clip.md`, ...) supplies only its own
register fields, footer fields, function, tests and targets. Where the
function spec and this frame disagree, the function spec wins.

## F1. Reference design and flow

Build the design **the way Waveflow's `examples/stream_inband` (the streaming
polynomial) is built**: hook-first.  Read that example's source and its docs
pages (`docs/examples/stream_inband/`) before writing anything.  In
particular:

| Piece | In `stream_inband` | Yours | Written by |
| --- | --- | --- | --- |
| schemas | `PolyCmdHdr`, `PolyRespHdr`, `PolyError`, `CoeffArray` (`DataList` / `EnumField` / `DataArray`) | per the function spec | you |
| the function | `poly_eval`: the arithmetic, in float32, in the C++ operation order | per the function spec | you |
| the protocol model | `poly_stream_model`: the whole kernel as a pure function of its input stream | same structure | you |
| scenarios | `scenarios.py`: intents, stimulus, expected responses, the checker | per the function spec | you |
| the module | `PolyAccel(HostActivated)`, body-only (`cpp_body = "body"`): ports and `VitisRegMap` | same structure | you |
| kernel boundary | `gen/poly.hpp`, `gen/poly.cpp`: prototype, pragmas, register map, one call to the body | -- | **Waveflow** |
| kernel body | `poly_body_impl.tpp`: the whole kernel, in C++ | same structure | you |
| pysim model | `PolyAccel.body()`: a port wrapper around the function, plus `proc_latency` / `proc_ii` | calibrated to your RTL (F7) | you |
| C++ testbench | `poly_tb.cpp`: plays each scenario's stimulus, records the response | same structure | you |
| headers, serializers | `include/*.h`, including `bundle_tb.h` | -- | **Waveflow** |
| build | `poly_build.py`: a `BuildDag` ending in `summary` | same structure, using `run_dag_cli` | you |

The kernel boundary and everything under `include/` are **generated** and
must never be hand-edited.  If the Waveflow machinery cannot express
something you need, **stop and report it**.  Do not work around it by
hand-writing a generated file.

**Do not hand-pack words.** Every header, footer and sample burst is
serialized through its Waveflow schema or through the Waveflow array
utilities, in Python and in C++ alike.

## F2. Target

| Item | Value |
| --- | --- |
| Part | `xc7z020clg484-1` |
| Clock | 10 ns, default clock uncertainty |
| Tool | Vitis HLS 2025.1 |
| Stream word width | 32 bits (`in_bw = out_bw = 32`) |
| AXI-Lite width | 32 bits |

## F3. Protocol

This is the same as `stream_inband`, with the additions marked **(new)**.

1. The host writes the function's parameters into the **register map**, then
   writes `ap_start`. The parameters stay fixed for the whole kernel run.
2. The kernel loops, reading one **command header** per iteration from the
   input stream:

   | Field | Type | Meaning |
   | --- | --- | --- |
   | `cmd_type` | enum `DATA = 0`, `END = 1` | |
   | `tx_id` | uint16 | transaction ID |
   | `nsamp` | uint16 | number of input samples (0 for `END`) |

3. On `END` the kernel returns cleanly, with `halted = 0` and `error = 0`.
4. On `DATA` the kernel **first checks the parameters (new)**. If they are
   illegal, it halts with `BAD_PARAM` (F4) and emits nothing for this
   transaction.
5. Otherwise, for each `DATA` transaction the host then sends the sample
   burst, TLAST on its last word, if `nsamp > 0`. The kernel writes, in order:
   1. the **response header** (`tx_id` echo), TLAST on its last word;
   2. the **data burst**, one output per processed sample. TLAST goes on
      its **last emitted word, including when the input burst ended early
      (new)**. The burst is omitted entirely when zero samples were processed;
   3. **(new)** the **response footer**, TLAST on its last word. It carries
      `nsamp_read` (uint16, samples processed) followed by the function
      spec's per-transaction results.

   A footer is used rather than register fields because the loop handles
   many transactions per run, and a register can hold only the last one's
   results.
6. Parameters and state (for example, a filter's history) **do not carry
   from one `DATA` transaction to the next**, unless the function spec says
   otherwise.

## F4. Errors and halting

Register map status fields, as in `stream_inband`: `halted` (bit),
`error` (the error enum) and `tx_id` (the offending transaction). The error
enum keeps `PolyError`'s codes and adds one:

| Code | Name | Detected? |
| --- | --- | --- |
| 0 | `NO_ERROR` | |
| 1 | `TLAST_EARLY_CMD_HDR` | reserved, not detected (the generated header read discards TLAST) |
| 2 | `NO_TLAST_CMD_HDR` | reserved, not detected |
| 3 | `TLAST_EARLY_SAMP_IN` | yes |
| 4 | `NO_TLAST_SAMP_IN` | yes |
| 5 | `WRONG_NSAMP` | yes, as in `stream_inband` |
| 6 | `BAD_PARAM` | **(new)** yes, with the conditions set by the function spec |

On any detected error the kernel sets `halted = 1`, `error` and `tx_id`,
then **returns without flushing** the input, as `stream_inband` does. Before
halting on codes 3 to 5, it still emits the response header, the samples it
processed, and the footer (step 5 of F3). On code 6 it emits nothing.

- **Code 3.** TLAST arrived on sample word `j`, before the last expected
  word. Every sample in words `0..j` is processed, capped at `nsamp`. Where a
  sample spans two words, a half-received sample is discarded.
- **Code 4.** The last expected sample word arrived without TLAST. All
  `nsamp` samples are processed.

In the error scenarios, the host sends exactly these words:

- **Early TLAST on word `j`:** words `0..j` only.
- **Missing TLAST:** exactly the expected number of sample words, none with TLAST.
- **`BAD_PARAM`:** the full sample burst, which the kernel never reads.

A halted run can therefore leave input unread. A leftover-data warning from
csim in these scenarios is expected and is not a failure. The check is the
output stream plus the register-map status.

## F5. Stage 1: the specification (then STOP for review)

Stage 1 writes down **what** the accelerator must do, in executable form:

- **The schemas,** in `<name>.py`: command header, response header, footer,
  error enum, register-map parameter types.
- **The function, `<name>_eval`,** in `<name>.py`: the spec's arithmetic as a
  pure function, in the exact operation order and precision the C++ will use
  (that is what lets every comparison be bit-exact).  Pin it down with the
  function spec's **worked examples** -- values computed by hand, in a test --
  because everything downstream trusts it.
- **`scenarios.py`:** every scenario as a list of **intents**, from fixed
  seeds, including the malformed ones (F4); its stimulus; and its **expected
  response and register status, computed from the intent** -- not by running
  a model of the protocol.  Plus the checker: words, burst boundaries, TLAST
  flags and status, exactly.
- **`layout.md`:** the word-by-word layout of every header, footer and burst,
  obtained by **serializing instances with Waveflow**, not by reasoning about
  the schema.

Then show the checker **rejecting** at least two wrong outputs of your
choosing (for example, the right function with the wrong rounding, or an
error path that does not halt), and run the worked examples.

When Stage 1 is complete, **stop and summarize** it.  Do not write the
accelerator until the spec is approved.

## F6. Stage 2: the accelerator

```
<name>.py                 # + <name>_stream_model, the module (body-only) and its pysim
                          #   wrapper, the pysim testbench -- next to the frozen schemas
                          #   and <name>_eval
<name>_body_impl.tpp      # the whole kernel body, in C++
<name>_tb.cpp             # the C++ testbench
<name>_build.py           # the BuildDag, ending in `summary`
results/report.md
```

Rules for Stage 2:

- **Do not modify the Stage 1 artifacts:** the schemas, `<name>_eval`,
  `scenarios.py`, `layout.md`.  If you believe one is wrong, stop and explain
  why.
- **Do not relax an acceptance criterion.** If you cannot meet one, report
  the best value you reached and what limits it.
- Every scenario runs through the protocol model and csim; the well-formed
  ones through pysim; the timing scenario through cosim.
- Scenarios are **pre-loaded**, as Vitis csim requires: the testbench plays
  a scenario's whole stimulus before draining the outputs.  No scenario may
  make an input depend on an earlier output.  A design that needs that
  requires a `FreeRunMod` with the concurrent BFM testbench flow, and is out
  of scope for this frame.

## F7. The comparisons

Everything is checked against the **expected responses** of Stage 1, which
come from the scenarios' intent.  Two implementations agreeing with each
other proves nothing -- one person or one AI may have written both with the
same misunderstanding -- so each is checked against that third description
instead:

| Comparison | Catches | Measured by |
| --- | --- | --- |
| protocol model vs expected | the Python model misreads the protocol | the `check_model` step |
| pysim vs expected | the module's Python body diverges from the model | the `check_pysim` step |
| csim / cosim vs expected | the C++ body diverges | the `check_csim` and `check_cosim` steps |
| pysim timing vs cosim cycles | the timing model (`proc_latency`, `proc_ii`) is wrong | `validate_timing`, **tolerance 20 cycles** as in `stream_inband` |

The function spec adds absolute timing and resource targets on top of these.

## F8. Report

`results/report.md` contains one table with a row per acceptance criterion:
the criterion, the required value, the measured value, PASS/FAIL, and the
source of the number (build step, report file, VCD, or a check step).
Below the table, give:

- the final `proc_latency` and `proc_ii`, and how you calibrated them;
- anything in the spec you found ambiguous and how you resolved it;
- every place the Waveflow machinery got in your way, with the error message
  you saw.
