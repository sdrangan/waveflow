---
title: A systolic matrix multiply, measured
parent: Examples
nav_order: 21
has_children: true
example_dir: examples/systolic
summary: "The systolic engine (SystolicEngine) and the engine behind memory (SystolicEngineMem), each in its own harness: a driver and sinks, or a driver, a sink and one memory, generated from the module. Each workload runs in pysim and at RTL from the same scenario files, every output checked against the bit-exact model; the RTL's timing is read at the ports, with no waveform, and the resources from csynth's XML. What it shows: the engine at its input bound -- 171 cycles per 8×8×32 product with 64-bit words, 94 with 128-bit, 81 an output batched -- how the binding stage was found, and the wrapper bound by its read port, 3,033 cycles for 32×32×64."
---

# A systolic matrix multiply, measured

[The systolic matrix multiply](../../guide/dsp/systolic/index.md) in the guide describes two modules:
`SystolicEngine`, which multiplies complex fixed-point matrices fed on streams, and
`SystolicEngineMem`, which runs it in an outer loop over matrices in memory. This example puts each in
its own harness and measures it at RTL: how long a product takes, against the bounds
[the theory](../../guide/dsp/systolic/systolic_theory.md#three-bounds-on-a-panel) predicts, which stage
sets the pace, and what it costs.

## Learning objectives

What this adds to the examples before it, in the order the pages teach it:

1. Put a composite kernel in **its own harness** (`harness(module)`) and run a workload in pysim and at
   RTL from the same scenario files, every output checked against the model.
2. Read a **steady interval** and a **completion cycle** from the sinks' arrival cycles, with no
   waveform.
3. **Calibrate** a task's timed phase inside a composite kernel at RTL, and score it on held-out
   shapes.
4. **Predict** a design's bound -- compute, input or output -- and compare it with the RTL.
5. **Find the stage that binds** from a trace: how long a block is held, and the gaps in an input's
   acceptances.
6. Read a design's **resources** from csynth's XML and compare configurations.

## The design

```
StreamDriver (s_in)  ->  SystolicEngine     ->  StreamSink (s_out), StreamSink (m_status)

StreamDriver (s_job) ->  SystolicEngineMem  ->  StreamSink (s_done)
                               ↕  m_axi read, m_axi write
                             memory (one arena: A, B and C)
```

| | |
|---|---|
| configuration | the guide's centre: `Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`, form 4 |
| formats | 12-bit complex operands (`A`, `B`, `C` with 3, 4 and 5 integer bits), 16-bit lanes |
| words | 64 and 128 bits (two and four elements a word) |
| target | `xczu48dr-ffvg1517-2-e` at 250 MHz (4 ns); Vitis HLS / Vivado xsim **2025.1** |

The task bodies are the framework's (`waveflow/build/systolic_engine_*_task.h`); the tops come from
`composite_top_spec` on the modules and the testbenches from the harness graphs. Build output lands
under the root a run is given.

## Running it

```bash
python -m examples.systolic.engine_harness --root C:/wfsc/eh --workload steady --word-bits 128
python -m examples.systolic.engine_mem_harness --root C:/wfsc/em --workload big --word-bits 128
pytest tests/examples/test_systolic.py                  # pysim against the model, both word widths
pytest tests/examples/test_systolic.py -m xsi           # the RTL gates (needs Vitis and Vivado)
python -m examples.systolic.systolic_measure --root C:/wfsc/sm    # every run -> measured.json
python -m examples.systolic.engine_harness --root C:/wfsc/cal --word-bits 128 --calibrate --through calib_phases
python -m examples.systolic.systolic_sweep --root C:/wfsc/sw      # csynth 15 configurations -> resources.json
```

`--through golden` stops after pysim; the default, `compare`, adds csynth and XSI. Build from a short
root on Windows: past 260 characters a csynth path fails silently.

## The pages

- [The harnesses](test.md) -- the two harnesses, the workloads, how the RTL is timed, the gates.
- [Timing](timing.md) -- the engine against its bounds, how the binding stage was found, the wrapper
  and its read port, pysim against the RTL.
- [Resources](resource.md) -- what each costs at 64- and 128-bit words.

The core's timed phase is **calibrated** at RTL ([Timing](timing.md#the-cores-timed-phase-calibrated)),
and pysim runs within 5 to 12% of the RTL; a resource model over the engine's parameters is the next
step, fitted on the sweep's builds ([Resources](resource.md#across-configurations)).
