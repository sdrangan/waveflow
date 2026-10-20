---
title: Simulation speed
parent: Evaluation
nav_order: 1
has_children: true
summary: "Is Waveflow's Python simulation fast enough to be the design loop? The claim, the cost model (pysim time = the untimed model's time + SimPy events x a per-event cost), how it is measured, and the results on memcpy, the SSR FFT, the processor model and the linear-algebra units: the flat FFT pysim costs 1.22x the untimed bit-exact model and runs about 57x faster than RTL simulation; the processor model's pysim is 33x faster than gem5 simulating the same software; the systolic and CG units' pysims are 17x and 29x faster than their RTL, before RTL's synthesis and build per design point (minutes for the FFT, about 40 s for each unit). What is still slow and what to do about it."
---

# Simulation speed

## The claim

One potential gain of Waveflow is a **faster simulation loop**. A system engineer evaluates an
algorithm in untimed Python; the hardware team then simulates RTL, which is accurate and slow.
Waveflow's loosely-timed pysim sits between them: the same bit-exact Python arithmetic, plus the
timing of the hardware -- latency, throughput, back-pressure -- without synthesizing anything.

That is only useful if the timing costs little on top of the arithmetic, and much less than RTL.
This section measures both, on blocks small enough to understand completely. It is an assessment,
not a demonstration: the goal is to know what the simulation costs and why, and where to improve
it.

## The cost model

The measurements so far fit a simple additive model:

```text
pysim time  =  untimed model time  +  (SimPy events) x (cost per event)
```

- **The untimed model time** is the arithmetic, and it is the *same code* in both runs: the pysim's
  tasks call the model's functions. Making the model faster makes the pysim faster by the same
  amount.
- **The events** are the transactions -- a message handed over, a memory read, a modelled delay.
  How many a transaction needs is a property of the model's structure.
- **The cost per event** is SimPy's own (about 0.6 us for a bare event on the test machine) plus
  Waveflow's work around it: building and parsing a message, interface bookkeeping. On memcpy it is
  about 16 us today (machine A, below); on the processor model about 2.7 us, and on the
  linear-algebra units about 24 us (machine B). It is a property of what an event carries: a
  processor call moves no data, a linear-algebra job moves matrices through typed messages.

So the honest questions are how many events a design needs, and what each one costs -- which is why
the first block measured is memcpy, which has no arithmetic at all.

## Results

| block | untimed model | Waveflow pysim | RTL (XSI) or gem5 | notes | machine |
|---|---|---|---|---|---|
| [memcpy](memcpy.md), per copy | 0.5 us | 0.4 ms (25 events) | -- | no compute: the per-transaction cost, flat in the copy length | A |
| [SSR FFT](fft.md) `L = 1024`, per frame | 1.87 ms (vectorized numpy) | 2.29 ms flat; 4.44 ms per task | 131 ms | plus ~131 s csynth and a 24.5 s XSI build per design point | A |
| [processor model](cpu.md), per scheduler operation | 2.77 us (Python + calibrated prices) | 14.7 us (4.46 events) | gem5 HPI: 481 us simulating it; 10.8 ms of a whole run, plus 5.5 s a run | three optimization rounds took the pysim from 31.8 us; warm prices (cold first pass 20.1 us) | B |
| [systolic unit](linalg.md) 8 x 8 by 8 x 32, per job | 0.044 ms | 1.73 ms (70.5 events) | 30.1 ms | plus 32 s csynth and a 10.9 s XSI build per design point | B |
| [CG vector unit](linalg.md) 8 iterations, per job | 1.97 ms | 11.6 ms (408.7 events) | 336 ms | plus 33 s csynth and a 9.9 s XSI build per design point | B |

- **Machine A**: Windows 11, an Intel 13th-generation-class CPU, Python 3.14; measured 2026-10-13.
- **Machine B**: Linux, an Intel Core i7-7700K (8 threads, 4.2 GHz), Python 3.12.3, SimPy 4.1.2;
  measured 2026-10-19, with Vitis HLS / Vivado 2024.1 and gem5 v25.1.

Machine A had nothing else running; machine B is shared, and its load average was below 1 before
each series. Best of several runs. The two machines differ in age and Python version, so compare
times within a machine; across machines, compare the ratios.

**Against a compiled model** the picture changes, and it should be read that way. The FFT's untimed
model is vectorized numpy -- a fair baseline, and what most system engineers write -- but not
compiled. Rewritten as typed loops and compiled, which is real work, it would take an estimated
~0.05 ms a frame. The pysim would call the same compiled arithmetic and keep its measured overhead,
~0.05 + 0.42 = ~0.47 ms flat: about 9 times the compiled model, still about 280 times faster than RTL.
The estimate and its basis are on [the FFT page](fft.md#with-a-compiled-model-an-estimate-not-yet-measured).

## How it is measured

- **Per-frame cost, not total.** Each fidelity runs at several frame counts and is fitted to
  `setup + frames x per_frame`, so building a graph or a simulator does not hide in the per-frame
  number. The one-time costs are reported beside it.
- **Same frames, same bits.** Every run of every fidelity is checked bit-exact against the golden.
- **Best of N.** Other load on a machine only ever adds time, so each point keeps its fastest run.
  When comparing two versions of the code, runs of the two alternate, each in a fresh process.
- **Outcomes, not just bits.** An optimization must leave the *simulated* outcome unchanged: when
  every message arrived, every task's firings. A faster simulation of something else is not a
  speedup.

## What is still slow

- **The cost per event**, about 16 us against SimPy's 0.6 us: mostly building and parsing small
  messages, and interface code. See [memcpy](memcpy.md#where-the-time-goes).
- **The untimed models themselves.** The FFT's bit-exact model is vectorized numpy, but does about
  a thousand int64 operations per sample, because it reproduces the vendor library step by step. A
  compiled model would lower the untimed baseline *and* the pysim. See [the FFT](fft.md#future-work).
- **Fine-grained designs.** A design that moves many small messages pays the per-transaction cost
  many times; memcpy's 0.4 ms per copy is about 800 times a numpy copy.
- **Typed messages and their interfaces.** In the linear-algebra units, building, packing and
  parsing messages and matrix blocks is 40% of the pysim, and the interfaces they cross another 18%;
  the units' own code is a few percent. The same framework path is what is left in memcpy and in the
  register traffic of the [processor model's](cpu.md#future-work) whole example. See
  [the linear-algebra units](linalg.md#why-it-was-not-optimized-here).

## The pages

- [memcpy](memcpy.md) -- the cost of a transaction, with no arithmetic to hide it.
- [The SSR FFT](fft.md) -- three ways to run the same bit-exact FFT, and the lessons from making
  its pysim fast.
- [The processor model](cpu.md) -- a scheduler's operations untimed, in the pysim and on gem5, and
  three rounds that halved the pysim's cost with every simulated outcome unchanged.
- [The linear-algebra units](linalg.md) -- the systolic and CG units against their bit-exact
  models and their RTL, and why their remaining cost is the framework's.
