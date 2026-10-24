---
title: "Simulation speed: memcpy"
parent: Simulation speed
nav_order: 1
summary: "The cost of one transaction in Waveflow's pysim, measured on memcpy, which has no arithmetic: about 0.4 ms and 25 SimPy events per copy whatever its length, against about 0.5 us for the same copy as a numpy slice. Where the time goes (message building and parsing, interface code, SimPy itself), how it came down from 94 events and 0.6 ms in one day, and what is left."
---

# Simulation speed: memcpy

memcpy has no arithmetic. Whatever its pysim costs is the cost of simulating transactions: the
cleanest place to measure it.

## The set-up

The [memcpy example](../../examples/memcpy/index.md), unchanged: a driver issues copy commands; a
sequencer turns each into an in-band read descriptor, write descriptor and response; a memory-read
stream and a memory-write stream move the data through a two-master AXI-MM crossbar to one shared
memory; a sink collects one done message per copy. A run is **N copies of B 64-bit words each**,
issued back to back (they overlap), every destination checked against its source.

```bash
python -m examples.mem_copy.mem_copy_runtime        # -> examples/mem_copy/mem_copy_runtime.json
```

## Results

| N copies | B words | pysim, total | per copy | SimPy events per copy | numpy copy, per copy |
|---|---|---|---|---|---|
| 1 | 16 | 14.9 ms | 14.9 ms | 39 | 0.6 us |
| 10 | 16 | 25.7 ms | 2.6 ms | 26 | 0.4 us |
| 100 | 16 | 68.1 ms | 0.68 ms | 25 | 0.4 us |
| 1000 | 16 | 407 ms | **0.41 ms** | 25 | 0.5 us |
| 100 | 1024 | 81.6 ms | 0.82 ms | 25 | 0.9 us |

Measured 2026-10-13 (the machine is described on [Simulation speed](index.md#results)).

**The cost is per transaction, not per word.** 100 copies of 1024 words cost about what 100 copies of
16 do: the data moves through the simulation as one numpy block per burst, and its length only sets
a computed delay. A single run also pays about 10 ms of fixed start-up.

**A copy is 25 SimPy events**: 13 modelled delays, 4 consumers waiting for data, 3 waits for room in
a full queue, and the crossbar's channel request and release (2 + 2). At 0.41 ms per copy that is
about 16 us per event, against about 0.6 us for a bare SimPy event on the same machine.

## Where the time goes

Profiled per copy, after the optimizations below:

| | share |
|---|---|
| building and parsing the small messages (the descriptors, the command, the response) | ~28% |
| generic Python inside Waveflow's code (`isinstance`, `len`, list appends) | ~19% |
| SimPy itself | ~16% |
| the stream and memory interfaces | ~21% |
| the mem-stream modules, parameter checks, the rest | ~16% |

So most of the cost per event is Waveflow's, not SimPy's -- which is where the next improvements are.

## How it got here

All on 2026-10-13, measured as interleaved A/B runs, every simulated outcome unchanged:

| step | events per copy | per copy |
|---|---|---|
| start of the day | 94 | ~0.6 ms |
| transfers inline rather than one spawned SimPy process each; schema caches | 68 | ~0.4 ms |
| the stream hop without events for requests SimPy grants on the spot | 25 | ~0.3-0.4 ms |

The second step is the general one. A SimPy resource decides a request when it is made: one that
can be satisfied is granted then, and its event only resumes the requester at a later step of the
same instant. A stream hop made five such requests -- the bus, room in the queue, the burst in, the
burst out, its words released. Doing what the grant does, and waking waiters as its callback would,
removes those events and changes no simulated time.

## What is left

- **The cost per event**: about 16 us against SimPy's 0.6. The share above says where: messages,
  then interfaces. A message plan per class already exists for flat messages; the remaining cost is
  creating message objects at all.
- **A transaction's 25 events** are close to the floor for this design: 13 of them are the modelled
  delays. Fewer would mean a coarser model of the same hardware.
- **Not measured yet**: designs with more masters on the crossbar, where requests do wait, and
  longer pipelines of small messages.
