---
title: Systolic harnesses
parent: A systolic matrix multiply, measured
nav_order: 1
summary: "The systolic engine's and its memory wrapper's harnesses: harness(module) builds a driver per stream input, a sink per output, and one memory behind the m_axi ports; the module names its workloads (test, steady, short; test, big) and its golden. Each run goes pysim, csynth, XSI, and compare -- every output word against the model -- and the RTL is timed from the BFMs' arrival cycles. The gates, fast and -m xsi."
---

# The harnesses

## One module, one harness

`harness(module)` ([unit harnesses](../../guide/flows/units_calibration.md)) turns a module into a
testbench graph with no hand-written testbench: a `StreamDriver` on each stream input, a `StreamSink` on
each output, and -- for `SystolicEngineMem` -- one memory behind both `m_axi` ports, loaded from the
workload's image and dumped after the run. The module supplies the rest:

| The module's | `SystolicEngine` | `SystolicEngineMem` |
|---|---|---|
| `workload(kind)` | the commands and operand bursts on `s_in` | the job descriptors on `s_job`, and the memory image with `A` and `B` block-major |
| `golden(kind)` | every `Resp`, `C` and `Status`, from `waveflow.linalg.matmul` | every `EngineDone`; `expected_c` gives each job's `C` words |

`examples/systolic/engine_harness.py` and `engine_mem_harness.py` add the build: the linear-algebra
headers, the module's free-running top (`composite_top_spec`), its csynth script, and the framework's
system steps (`add_system_steps`) -- pysim, csynth, the system top, XSI, compare.

## The workloads

| Module | Workload | What it runs | What it shows |
|---|---|---|---|
| engine | `test` | six commands: one tile; `Aᴴ`; three panels; `keep_a` with two outputs; `Aᴴ` with two panels and two outputs; `kp = 1` | bit-exactness, every path of the array |
| engine | `steady` | four `8×8×32` commands back to back | the steady interval |
| engine | `short` | four `8×1×32`, then four `8×2×32` | the output bound: `C` stays 256 values whatever `kp` |
| engine | `batch` | two commands of four `8×8×32` outputs each | a command's fixed costs spread over its outputs |
| engine | `calib` | every tile count, one and two panels, `Aᴴ`, `kp = 1` | the spread the core's `panel` phase is fitted on |
| wrapper | `test` | a 2×2-tile job of two panels, an `Aᴴ` job of four panels, one tile | `C` in memory, every path of the loop |
| wrapper | `big` | one `32×32×64` product: 8 tiles of 4 panels | the completion time |

## Timing the RTL without a waveform

Each sink's BFM records the cycle each beat arrives (`vectors/<port>/cycles.bin`). The engine's
interval is the distance between the last beats of consecutive outputs on `s_out`; the wrapper's time is
the arrival of its `EngineDone` on `s_done`, which the writer sends after the job's last tile of `C` is in
memory. `systolic_measure` reads both, and csynth's own XML for the resources, into `measured.json`.

To see *why* a run takes what it takes, `--trace` dumps a waveform of the top's channels, and
`waveflow.utils.trace.load_trace` reads it by the names in the top's trace manifest: when each
stream-of-blocks block is acquired and released, when each FIFO beat moves ([Timing](timing.md#finding-the-stage-that-binds)).

## Every run checks every output

`compare` checks the RTL's recording of every output port against pysim's and the golden, word for
word; for the wrapper, the harness's `check_c` compares each job's `C` in the dumped memory with the
model. A run that finished on time with a wrong word fails.

## The gates

| Test | Checks | Marker |
|---|---|---|
| `tests/examples/test_systolic.py` | the engine's pysim against the model on the `test` workload, and the wrapper's pysim `C`, at 64 and 128-bit words | none |
| `tests/examples/test_systolic.py` | the engine at RTL: bit-exact, and the `steady` interval -- 171 cycles a product with 64-bit words, 94 with 128-bit; the wrapper at RTL: `C` of the `big` product exact, at both widths | `xsi` |
| `tests/linalg/test_systolic_engine.py` | without a harness: the generated top's banked blocks and port widths, the command and job rules | none |
