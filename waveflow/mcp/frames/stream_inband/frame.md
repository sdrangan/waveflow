# Streaming accelerator frame (read this before the function spec)

This file is the part of every accelerator spec that stays the same from one
design to the next: the protocol, the error behavior, the build flow and the
process. Each function spec (`01_gain_clip.md`, ...) supplies only its own
command-header parameters, footer fields, function, tests and targets. Where
the function spec and this frame disagree, the function spec wins.

The protocol is the **command-response contract** of `stream_inband`: read its
seven numbered rules, and the reason for each, in
`docs/examples/stream_inband/index.md` (`waveflow_get_doc`) before anything
else. `docs/examples/stream_inband/decisions.md` lists the decisions your
Stage 1 spec must state.

## F1. Reference design and flow

Build the design **the way Waveflow's `examples/stream_inband` (the streaming
polynomial) is built**: hook-first.  Read that example's source and its docs
pages (`docs/examples/stream_inband/`) before writing anything.  In
particular:

| Piece | In `stream_inband` | Yours | Written by |
| --- | --- | --- | --- |
| schemas | `PolyCmdHdr` (carrying the `CoeffArray`), `PolyRespHdr`, `PolyError` (`DataList` / `EnumField` / `DataArray`) | per the function spec, plus a `RespFtr` if it has per-command results | you |
| the function | `poly_eval`: the arithmetic, in float32, in the C++ operation order | per the function spec | you |
| the protocol model | `poly_stream_model`: the whole kernel as a pure function of its input stream | same structure | you |
| scenarios | `scenarios.py`: intents, stimulus, expected responses, the checker | per the function spec | you |
| the module | `PolyAccel(HostActivated)`, body-only (`cpp_body = "body"`): ports and a status-only `VitisRegMap` | same structure | you |
| kernel boundary | `gen/poly.hpp`, `gen/poly.cpp`: prototype, pragmas, status registers, one call to the body | -- | **Waveflow** |
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

This is `stream_inband`'s contract, with the additions marked **(new)**.

1. The host writes `ap_start`. **Nothing else is written over AXI-Lite**:
   the function's parameters travel in every `DATA` command header (rule 2),
   and the register map holds only the status.
2. The kernel clears its status, then loops, reading one **command header**
   per iteration from the input stream:

   | Field | Type | Meaning |
   | --- | --- | --- |
   | `cmd_type` | enum `DATA = 0`, `END = 1` | |
   | `tx_id` | uint16 | command ID |
   | `nsamp` | uint16 | number of input samples (0 for `END`) |
   | *parameters* | per the function spec | the function's parameters for this command; zeros, and ignored, on `END` |

3. On `END` the kernel returns cleanly, with `halted = 0`, `error = 0` and
   `tx_id = 0`.
4. On `DATA` the kernel **first checks the parameters in the header (new)**.
   If they are illegal, it halts with `BAD_PARAM` (F4) and writes nothing for
   this command.
5. Otherwise the host sends the sample burst, TLAST on its last word, if
   `nsamp > 0`. The kernel writes, in order:
   1. the **response header** (`tx_id` echo), TLAST on its last word;
   2. the **data burst**, one output per processed sample, TLAST on its last
      word. The burst is omitted entirely when `nsamp = 0`;
   3. **(new)** the **response footer**, if the function spec defines one,
      TLAST on its last word. It holds only per-command results that are known
      after the data (a count, a peak). **It is sent only when the command
      succeeds**, never after an error.
6. Nothing carries over from one `DATA` command to the next -- parameters,
   state (for example, a filter's history), anything -- unless the function
   spec says otherwise.

## F4. Errors and halting

Status registers, as in `stream_inband`: `halted` (bit), `error` (the error
enum) and `tx_id` (the command that failed). The error enum keeps
`PolyError`'s codes and adds one:

| Code | Name | Raised when |
| --- | --- | --- |
| 0 | `NO_ERROR` | |
| 1 | `TLAST_EARLY_SAMP_IN` | TLAST arrives before the last sample word |
| 2 | `NO_TLAST_SAMP_IN` | the last sample word has no TLAST |
| 3 | `BAD_PARAM` | **(new)** the header's parameters are illegal, per the function spec |

On any error the kernel (rule 6 of the contract):

1. sets `halted = 1`, `error` and `tx_id`;
2. puts TLAST on the last word it wrote, if it had started an output burst --
   so a burst that ends early is still closed;
3. returns at once, **reading nothing more** from the input stream. It does
   not drain to the next TLAST and writes no footer.

After an error the contents of the input stream are undefined (rule 7): the
host resets the stream path before the next `ap_start`.

- **Code 1.** TLAST arrived on sample word `j`, before the last expected
  word. Every sample in words `0..j` is processed, capped at `nsamp`, and the
  output word for word `j` carries TLAST. Where a sample spans two words, a
  half-received sample is discarded.
- **Code 2.** The last expected sample word arrived without TLAST. All
  `nsamp` samples are processed; the last output word carries TLAST.
- **Code 3.** Detected from the header, before anything is written for the
  command. The command's sample burst is never read.

In the error scenarios, the host sends exactly these words for the failing
command, and may queue further commands behind it:

- **Early TLAST on word `j`:** words `0..j` only.
- **Missing TLAST:** exactly the expected number of sample words, none with TLAST.
- **`BAD_PARAM`:** the full sample burst, which the kernel never reads.

A halted run therefore leaves input unread. The C++ testbench discards it
after the kernel returns -- that is the host's reset -- so a leftover-data
warning from csim is expected and is not a failure. The check is the output
stream plus the status registers.

## F5. Stage 1: the specification (then STOP for review)

Stage 1 writes down **what** the accelerator must do, in executable form:

- **The schemas,** in `<name>.py`: command header (with the function's
  parameters), response header, footer (if any), error enum.
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
- **The decisions** of `docs/examples/stream_inband/decisions.md`, each
  answered in one line: zero-length commands, error precedence, packing,
  rounding and saturation, what `tx_id` means, the footer.

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
