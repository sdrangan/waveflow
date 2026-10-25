---
title: Simulation speed
parent: Evaluation
nav_order: 1
has_children: true
summary: "Is Waveflow's Python simulation fast enough to be the design loop? The claim, the cost model (pysim time = the untimed model's time + SimPy events x a per-event cost), how it is measured, and the results on memcpy, the SSR FFT and the processor model: the flat FFT pysim costs 1.22x the untimed bit-exact model and runs about 57x faster than RTL simulation, before RTL's minutes of synthesis and build per design point; the processor model's pysim is 33x faster than gem5 simulating the same software. What is still slow and what to do about it. The systolic engine and the vector unit's two programs (CG and a matched filter) follow, across problem sizes."
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
  about 16 us today; on the processor model about 2.7 us, measured on another machine (below). It is a
  property of what an event carries: a processor call moves no data.

So the honest questions are how many events a design needs, and what each one costs -- which is why
the first block measured is memcpy, which has no arithmetic at all.

## Results

| block | untimed model | Waveflow pysim | RTL (XSI) or gem5 | notes |
|---|---|---|---|---|
| [memcpy](memcpy.md), per copy | 0.5 us | 0.4 ms (25 events) | -- | no compute: the per-transaction cost, flat in the copy length |
| [SSR FFT](fft.md) `L = 1024`, per frame | 1.87 ms (vectorized numpy) | 2.29 ms flat; 4.44 ms per task | 131 ms | plus ~131 s csynth and a 24.5 s XSI build per design point |
| [processor model](cpu.md), per scheduler operation | 2.77 us (Python + calibrated prices) | 14.7 us (4.46 events) | gem5 HPI: 481 us simulating it; 10.8 ms of a whole run, plus 5.5 s a run | three optimization rounds took the pysim from 31.8 us; warm prices (cold first pass 20.1 us) |
| [systolic engine](systolic.md) `8×8×32`, per product (preliminary) | 0.036 ms | 0.73 ms (44 events) | 43 ms | plus ~80 s csynth and a 20 s XSI build per design point |
| [CG on the vector unit](vecunit_cg.md), per `8×32` job of three iterations | 0.05 ms (`cg_solve`); 7.4 ms the unit's interpreter | 15 ms, of which 8.1 ms builds the harness (104 events) | 3.9 s | plus ~98 s csynth and a 93 s XSI build per design point |
| [matched filter on the vector unit](vecunit_mf.md), per 64-sample chunk at 8 antennas | 0.009 ms (numpy); 0.35 ms the unit's interpreter | 0.92 ms (34 events) | 159 ms | plus ~89 s csynth and an 85 s XSI build per design point |

Measured 2026-10-13 on one Windows 11 machine (Intel 13th-generation-class CPU, Python 3.14),
nothing else running, best of several runs. The processor-model row was measured 2026-10-19 on a
Linux workstation (Intel Core i7-7700K, 8 threads at 4.2 GHz, Python 3.12.3, SimPy 4.1.2) with gem5
v25.1, its load average below 1 before each series; the two machines differ in age and Python
version, so compare times within a machine, and across machines the ratios. The two vector-unit rows
were measured 2026-10-24 on a
Linux workstation (Intel Core i7-7700K, Python 3.12) with Vitis HLS / Vivado xsim 2024.1, otherwise
idle, each fitted over four problem sizes, after Stage 10 of `plans/vecunit_new.md` made the unit's
own Python faster.

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

## The pages

- [memcpy](memcpy.md) -- the cost of a transaction, with no arithmetic to hide it.
- [The SSR FFT](fft.md) -- three ways to run the same bit-exact FFT, and the lessons from making
  its pysim fast.
- [The processor model](cpu.md) -- a scheduler's operations untimed, in the pysim and on gem5, and
  three rounds that halved the pysim's cost with every simulated outcome unchanged.
- [The systolic engine](systolic.md) -- preliminary: a tiny model beside a timed simulation, and
  where its pysim time goes.
- [CG on the vector unit](vecunit_cg.md) -- an iterative program on the programmable vector unit:
  the unit's interpreter is the untimed term; its pysim made 3 to 5 times faster with no result
  changed.
- [The matched filter on the vector unit](vecunit_mf.md) -- a streaming program on the same unit:
  small blocks, so the timing costs more against the arithmetic.
