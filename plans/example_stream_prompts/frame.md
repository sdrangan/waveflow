# Streaming accelerator frame (read this before the function spec)

This file is the part of every accelerator spec that stays the same from one
design to the next: the protocol, the error behavior, the build flow and the
process. Each function spec (`01_gain_clip.md`, ...) supplies only its own
register fields, footer fields, function, tests and targets. Where the
function spec and this frame disagree, the function spec wins.

## F1. Reference design and flow

Build the design **the way Waveflow's `examples/stream_inband` (the streaming
polynomial) is built**, using the full Waveflow machinery. Read that
example's source and its docs pages (`docs/examples/stream_inband/`) before
writing anything. In particular:

| Piece | In `stream_inband` | Yours |
| --- | --- | --- |
| schemas | `PolyCmdHdr`, `PolyRespHdr`, `PolyError`, `CoeffArray` (`DataList` / `EnumField` / `DataArray`) | per the function spec |
| accelerator | `PolyAccel(HostActivated)`: `VitisRegMap` + `on_start` persistent loop | same structure |
| compute | `@synthesizable evaluate(...)` in Python + hand-written C++ hook `poly_evaluate_impl.tpp` | same structure |
| timing model | `proc_latency`, `proc_ii` on the module | calibrated to your RTL (F7) |
| pysim testbench | `PolyTB(SimObj)` | same structure |
| C++ testbench | generated from `PolyTBHls(SeqTB)` | same structure |
| build | `poly_build.py`: a `BuildDag` of named steps | same structure, using `run_dag_cli` |

The following are **generated** and must never be hand-edited: the kernel
C++, the testbench C++, and every header under `include/`. The only C++ you
write is the compute hook, plus a helper hook if you need one. If the
Waveflow machinery cannot express something you need, **stop and report it**.
Do not work around it by hand-writing a generated file.

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

## F5. Stage 1: the specification artifacts (then STOP for review)

```
spec/
  <name>_schemas.py   # every schema: command header, response header, footer, error enum,
                      # register-map parameter types
  oracle.py           # an INDEPENDENT reference: plain numpy, no Waveflow HwModule, no SimPy.
                      # Implements the function spec's exact function, F3/F4 framing and
                      # errors, and the expected register-map status after a run.
  scenarios.py        # builds every test scenario from fixed seeds into spec/vectors/<scenario>/:
                      #   register-map parameters, the full input stream (all commands + samples,
                      #   including malformed framing), and the oracle's expected output stream
                      #   and status
  check.py            # check.py <results_dir>: compares a run's outputs to the oracle, per scenario,
                      # one PASS/FAIL line per criterion; exits nonzero on any failure
  layout.md           # word-by-word layout of every header, footer and sample burst, obtained by
                      # SERIALIZING instances with Waveflow, not by reasoning about the schema
```

- `oracle.py` must **not** import the accelerator module, which does not
  exist yet. That independence is the point. Stage 2's `HwModule` is checked
  against it.
- Run `oracle.py` on the function spec's worked examples and show the result.
- `check.py` must be shown to **reject** at least two wrong outputs of your
  choosing. Name the mutants and the output of each.

When Stage 1 is complete, **stop and summarize** it. Do not write the
accelerator until the spec is approved.

## F6. Stage 2: the accelerator

```
<name>.py                 # the HwModule, pysim testbench and SeqTB, importing spec/<name>_schemas.py
<name>_<method>_impl.tpp  # the hand-written compute hook
<name>_build.py           # BuildDag via run_dag_cli: build_inputs (from spec/vectors) -> py_sim ->
                          # gen_include -> gen_kernel -> gen_tb -> csim -> validate_csim -> csynth ->
                          # inspect_synth -> cosim -> timing steps; plus a step that runs spec/check.py
                          # on the pysim, csim and cosim results
results/report.md
```

Rules for Stage 2:

- **Do not modify anything under `spec/`.** If you believe the spec, the
  oracle or a scenario is wrong, stop and explain why.
- **Do not relax an acceptance criterion.** If you cannot meet one, report
  the best value you reached and what limits it.
- Every scenario runs through pysim and csim. The timing scenario also runs
  through cosim. If the `SeqTB` cannot express a scenario, report which one
  and why.
- Scenarios are **pre-loaded**, as Vitis csim requires: the testbench pushes
  every command and sample burst of a scenario (for example `DATA`, `DATA`,
  `END`) before the kernel runs, then drains the outputs. No scenario may
  make an input depend on an earlier output. A design that needs that
  requires a `FreeRunMod` with the concurrent BFM testbench flow, and is out
  of scope for this frame.

## F7. The three comparisons

A design is accepted only when **all three** pass. Each one catches a
different kind of mistake:

| Comparison | Catches | Measured by |
| --- | --- | --- |
| oracle vs pysim | the Python model misreads the spec | `spec/check.py` on the pysim results |
| pysim vs csim/cosim | the C++ hook does not match the Python model | `validate_csim`, plus `check.py` on the csim and cosim results |
| pysim timing vs cosim cycles | the timing model (`proc_latency`, `proc_ii`) is wrong | the build's timing validation, **tolerance 20 cycles** as in `stream_inband` |

The function spec adds absolute timing and resource targets on top of these.

## F8. Report

`results/report.md` contains one table with a row per acceptance criterion:
the criterion, the required value, the measured value, PASS/FAIL, and the
source of the number (build step, report file, VCD, or `check.py` output).
Below the table, give:

- the final `proc_latency` and `proc_ii`, and how you calibrated them;
- anything in the spec you found ambiguous and how you resolved it;
- every place the Waveflow machinery got in your way, with the error message
  you saw.
