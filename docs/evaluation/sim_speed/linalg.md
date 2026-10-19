---
title: "Simulation speed: the linear-algebra units"
parent: Simulation speed
nav_order: 4
summary: "The systolic matrix multiply and the CG vector unit, each in its memory-fed bench, run three ways on the same jobs: the bit-exact model (0.044 and 1.97 ms a job), Waveflow's pysim (1.73 and 11.6 ms) and the synthesized RTL under XSI (30.1 and 336 ms, plus about 32 s of csynth and a 10 s build per design point). Both pysims pay about 24 us per SimPy event on top of the arithmetic, and keep the RTL's job interval within 1 %. Almost all of the pysim's time is the framework's messages and interfaces, not the units: why it was not optimized here, and what would make it faster."
---

# Simulation speed: the linear-algebra units

The linear-algebra components are a third kind of block: not a stream of samples like the
[FFT](fft.md), not software like the [processor model](cpu.md), but units that take a request
message, compute on whole matrices held in blocks, and answer with a reply message. Their pysim
bodies call the bit-exact models and charge calibrated timing. This page measures what that costs.

## The set-up

Each unit runs standalone in the memory-fed bench its own tests use: a request framer reads each
job's operands from memory through the framework's memory streams, the unit computes, a reply framer
writes the result back, all through one AXI-MM crossbar and one memory. Both are at the centre
configuration of their tests, on N = 10, 50 and 100 identical full-size jobs:

- **the [systolic unit](../../guide/dsp/systolic/index.md)**: `C = A·B`, an 8 x 8 by 8 x 32 complex
  multiply on a 4 x 8 array, four lanes, one request and one reply a job;
- **the [CG vector unit](../../guide/dsp/cg_vector.md)**: eight iterations of conjugate gradient on
  an 8 x 8 system with 32 right-hand sides, four lanes: a `START` and eight `STEP` requests a job,
  each answered, the `S` matrices given (from the model), as they would come from a systolic array.

```text
untimed:  jobs --> matmul, or cg_init + 8 x vec_step (the bit-exact models) --> results
pysim:    driver --> framer --> memory stream --> the unit (receive, load, core, store) --> memory stream --> sink  (SimPy)
RTL:      the bench's synthesized top (Vitis HLS 2024.1) --> XSI, driven by the generated BFM testbench
```

- **Untimed** is the bit-exact model alone, on the same operands: the functions the pysim's core
  calls. Its results are checked against the bench's expected replies, word for word.
- **The pysim** is timed around `run_sim()` alone. Building the bench and writing its scenario is
  reported apart, as *construction*, because the CG bench computes its golden replies while it is
  built.
- **The RTL** is the same scenario through the bench's synthesized top under XSI, every reply
  checked. The testbench runs for a fixed number of cycles, set 5 % past the pysim's end time.

```bash
python -m tests.linalg._sim_speed --rtl --synth --profile   # -> tests/linalg/sim_speed_runtime.json
```

## Results

Per job, fitted over 10, 50 and 100 jobs; every run bit-exact:

| | systolic unit | CG vector unit | one-time cost |
|---|---|---|---|
| untimed (bit-exact model) | **0.044 ms** | **1.97 ms** | none |
| pysim | **1.73 ms** (39x) | **11.6 ms** (5.9x) | building the bench: 0.34 / 6.81 ms a job (golden included) |
| RTL under XSI | **30.1 ms** (17x the pysim) | **336 ms** (29x the pysim) | csynth 32 / 33 s; an XSI build of 10.9 / 9.9 s a run |

| also measured | systolic | CG |
|---|---|---|
| simulated time a job, pysim (at 250 MHz) | 1.55 us, 388 cycles | 37.30 us, 9,324 cycles |
| the RTL's interval between jobs | 390 cycles | 9,231 cycles |
| SimPy events a job | 70.5 | 408.7 |
| RTL time pro-rated to the last reply (a lower bound, see below) | 28.7 ms | 317 ms |

Measured 2026-10-19 with Vitis HLS / Vivado 2024.1 for the RTL, on the machine of the
[processor model page](cpu.md#results) (Linux, Intel Core i7-7700K, Python 3.12.3, SimPy 4.1.2): the
machine idle before each series, the best of three runs per point, the RTL best of one, as on the
FFT page. The XSI build is most of a run at these sizes (its elaborate step alone is 8.0 and 6.9 s),
and every run pays it: the runner rebuilds each time.

**The pysim keeps the RTL's timing.** Its interval between systolic jobs is 388 cycles against the
RTL's 390, and between CG jobs 9,324 against 9,231 (+1.0 %): the calibrated timing of the units, not
a coarser stand-in.

## Where the time goes

Profiled at 100 jobs (cProfile, so the shares are indicative), time in numpy and the standard library
charged to the code that called it:

| | systolic, pysim | CG, pysim |
|---|---|---|
| words and schemas: building, packing and parsing messages and matrix blocks | 40% | 38% |
| stream and block interfaces | 18% | 17% |
| SimPy itself | 16% | 14% |
| memory, memory streams, the bench's framers | 7% | 7% |
| the model arithmetic (`matmul`, CG, fixed point) | 3% | 17% |
| timing models, the units' task bodies, the framework, the rest | 15% | 8% |

The untimed models are almost all arithmetic: 97% (systolic) and 99% (CG).

So the pysim's cost is what moves the data, not what computes on it: about **24 us per SimPy
event** in both units (1.68 ms over 70.5 events, 9.68 ms over 408.7). The systolic unit computes
little per message -- one 8 x 8 by 8 x 32 product is 44 us of numpy -- so the messages are almost
all of it, 39x; CG computes a CG iteration per message, so the same overhead is 5.9x. It is the
picture the [memcpy page](memcpy.md#where-the-time-goes) drew with no arithmetic at all: the cost
of a transaction is building and parsing small messages, and the interfaces they cross.

## Why it was not optimized here

The [processor model](cpu.md#how-it-got-here) went through three optimization rounds; these units
went through none. Their task bodies and timing models are 7% of the systolic pysim and 3% of CG's,
and their arithmetic 3% and 17% -- most of CG's in the shared fixed-point library. Everything above
that is shared framework: the typed message path of `waveflow/hw/dataschema.py` and
`waveflow/hw/interface.py`, used by every example and by the scenario writers of the RTL flow.
Three changes there were proposed and set aside for a change of their own, with the shares that
motivate them in [future work](#future-work). A fast path in the units' own lane packing would have
meant packing words by hand, outside the schema that owns their layout, which Waveflow does not do.

## Insights

- **A job of the systolic unit costs 1.73 ms to simulate and 30.1 ms at RTL**, and the RTL also needs
  32 s of csynth and an 11 s build for every design point. Exploring designs, rather than workloads,
  is where that gap matters most.
- **The overhead is per event and the same in both units.** What differs is how much arithmetic each
  message carries: 3% of the systolic pysim, 17% of CG's.
- **Construction is not free.** The CG bench spends 6.81 ms a job computing its golden replies --
  more than half of what the pysim itself takes. A timer around a bench's whole run would have
  charged it to the simulation.
- **The memory-fed bench is part of what is measured.** Its framers, memory streams and memory are
  7% of the pysim. A unit fed straight from streams would pay less; the benches here are the ones
  the RTL gates use, so the two fidelities see the same graph.

## Lessons learned

1. **Keep the golden out of the timer.** Both benches compute their expected replies when they are
   built or their scenario is written; time `run_sim()` alone and report the rest apart.
2. **Size an RTL run tightly.** The testbench runs a fixed number of cycles. Sized a quarter past the
   pysim's end, a fifth of its cycles were idle -- and cutting them saved far less time than their
   number suggested, because idle cycles are cheap for XSI. Pro-rating by cycles therefore
   under-states the RTL; sizing the run 5 % past the end, which the pysim's 1 % accuracy allows, keeps
   the error small, and a run cut short fails its bit check.
3. **The RTL flow needs its environment.** On this machine the XSI runner finds Vivado and its
   simulation kernel only from the activated virtual environment (`XILINX_VIVADO`, and a link from the
   kernel's 2025.1 name to 2024.1's); called without it, every gate errored.
4. **Charge library time to its callers.** A profile that leaves numpy's own Python as "other" hides
   a third of a model's arithmetic.

## Future work

- **The typed message path** (`interface._unpack`, and the deserialization and construction of each
  message) and **the matrix blocks' (de)serialization wrapper** around their numpy packing: between
  them, most of the 40% the words and schemas take.
- **The fixed-point arithmetic** (`waveflow/utils/fixputils.py`): most of the CG model's own time,
  and so of its untimed baseline and its pysim alike.
- **Other configurations and harnesses**: larger arrays, the systolic unit's stream-fed harness, and
  the two units together in the [MIMO CG detector](../../examples/mimo_cg/index.md). The systolic unit
  is being redesigned for an outer loop; its numbers here are of commit `f3768657` and should be
  measured again after it.
