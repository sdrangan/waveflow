---
title: Simulation speed
parent: Evaluation
nav_order: 1
has_children: true
summary: "Is Waveflow's Python simulation fast enough to be the design loop? The claim, the cost model (pysim time = the untimed model's time + SimPy events x a per-event cost), how it is measured, and the results on memcpy and the SSR FFT: the flat FFT pysim costs 1.22x the untimed bit-exact model and runs about 57x faster than RTL simulation, before RTL's minutes of synthesis and build per design point. What is still slow and what to do about it."
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
  about 16 us today.

So the honest questions are how many events a design needs, and what each one costs -- which is why
the first block measured is memcpy, which has no arithmetic at all.

## Results

| block | untimed model | Waveflow pysim | RTL (XSI) | notes |
|---|---|---|---|---|
| [memcpy](memcpy.md), per copy | 0.5 us | 0.4 ms (25 events) | -- | no compute: the per-transaction cost, flat in the copy length |
| [SSR FFT](fft.md) `L = 1024`, per frame | 1.87 ms (vectorized numpy) | 2.29 ms flat; 4.44 ms per task | 131 ms | plus ~131 s csynth and a 24.5 s XSI build per design point |

Measured 2026-10-13 on one Windows 11 machine (Intel 13th-generation-class CPU, Python 3.14),
nothing else running, best of several runs.

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
