---
title: Timed phases
parent: Timing Models
nav_order: 5.5
audience: python
api: [TimedPhase, TimingEvent, HwModule.add_timing_phase, TimedPhase.timeout, TimedPhase.pipeline, StreamIFSlave.req]
summary: "Declare how long a module's work takes as phases between events at its own ports -- the first or last beat of a transfer -- with the features the duration depends on and a learned model. The body reads and writes as usual and lets the phase wait. Two patterns: sequential (one span) and pipelined (latency + fill). From the declarations alone the framework logs every transfer in pysim, reads the same transfers off an RTL trace, pairs them, and fits the model."
---

# Timed phases

A module's timing is stated as **phases** between **events at its own ports**, declared once beside
its endpoints.  Its body reads and writes as it always has; between a read and a write it lets the
phase wait.  Nothing in the body computes a time.

Two rules make a phase calibratable:

1. **A phase is the time between two events whose duration is a deterministic function of features
   known before the first event.**  Its *timing model* learns that function: the targets are the
   observable durations, and the model's parameters (a latency, a rate) stay inside it.
2. **Every event is measurable.**  An event is an endpoint's `first` or `last` beat of a transfer --
   a handshake an RTL trace shows -- and pysim's transfer primitive logs the same event.

## Declaring a phase

An endpoint provides its events: `self.s_in.first`, `self.s_in.last`.  A phase names its **spans**,
each a pair of events, the **features** its durations depend on, and optionally **seed** numbers --
what it predicts before it is fitted.  It is registered like an endpoint:

```python
self.compute_phase = TimedPhase(
    "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)},
    features=("n",), seed={"compute_time": {"intercept": 3.0, "n": 2.0}})
self.add_timing_phase(self.compute_phase)
```

- A span uses only the module's **own** endpoints.  A span between two modules belongs to the
  composite holding both.
- An event names an **operation on an endpoint**, never a role ("load") and never a message: which
  transfer it refers to is bound at run time.
- Durations are in **cycles** of the module's clock, so a fit is clock-independent.
- A phase and its model are one object: `phase.tm` holds one model per span (linear in the features
  by default), and `phase.predict(n=4)` works with no simulator.

## Pattern 1: sequential

The input is complete before the compute starts, and the output starts after it -- one span, from the
input's `last` to the output's `first`:

```python
x = yield from self.s_in.get()                 # returns at s_in.last
y = compute(x)
yield self.compute_phase.timeout(n=len(x))     # waits compute_time; opens the record
yield from self.m_out.write(y)                 # its first beat closes the record
```

`timeout` measures the span from the start endpoint's most recent transfer and binds its end to the
next transfer on the end endpoint.  A body that waits before reading, or passes a feature the phase
does not declare, is refused.  Declaring this span also says the body is sequential: a burst that sat
in the input buffer is read at a word a cycle, as the RTL reads it.

## Pattern 2: pipelined

The HLS loop that reads a word, computes, writes a word: the output starts a **latency** after the
**first** input word, then comes at the loop's rate.  Two spans:

```python
self.samp_phase = TimedPhase(
    "samples", spans={"latency":   (self.s_in.first, self.m_out.first),
                      "fill_time": (self.m_out.first, self.m_out.last)},
    features=("nwords",))
self.add_timing_phase(self.samp_phase)

# the body, per message
r = yield from self.samp_phase.pipeline(compute, load=self.s_in.req(nwords_max=n),
                                        store=self.m_out, nwords=n)
if r.load.nwords < n:            # the burst ended early, at TLAST
    ...
```

`pipeline` returns what it did: `r.y` is `compute`'s result (what was written), `r.load` and
`r.store` the two transfers.  A body that decides from its input -- poly halts when a sample burst
ends early -- reads `r.load`, so `compute` stays a pure function of the words.

`pipeline` takes the words at the load's first beat, starts the store a latency later, and ends it no
earlier than its fill nor the last input word plus the latency.  Every wait is a real event plus a
prediction; nothing is backdated, so a shared bus is held when it is really used.  Streaming versus
block is only which event a span starts at: a block process's span starts at the input's `last`.

## Calibrating a phase

From the declarations alone (`waveflow/calib/phases.py`):

- the **ports** to observe -- each endpoint a phase names, under its attribute name, which is its RTL
  port;
- the **RTL transfers** on them, decoded from a trace of the kernel's ports (TLAST frames a transfer);
- the **pairing**: pysim's phase records say which transfers each span starts and ends on, and with
  which features; the RTL supplies the times.  Both sides must carry the same transfers on every port,
  so functional equivalence is a precondition of a timing fit;
- the **fit**, per span, on the rows the RTL did not stall, with interior points held out.  A
  pipelined occurrence whose load delivered fewer words than it asked for (a burst that ended early) is
  marked `short` and left out: its features describe the request, not what moved.

The one hand-written piece is the scenario that produces the features.  Poly's is a run of commands of
nine lengths (`examples/stream_inband/poly_calibrate.py`); at 32 bits it
recovers a 4-cycle response, a 34-cycle latency and a fill of `nwords - 1` cycles, and predicts its two
held-out lengths exactly.

What a phase does not cover is not timed.  Poly's transaction also spends cycles before its first
header beat, after its last result, and in the overlap of its response with its first sample word --
none of them in a declared span -- so its fitted spans are exact while its end-to-end count is a few
cycles short.  An undeclared gap is where to look when the spans agree and the total does not.

## See also

- [Streaming processing](./streaming.md) -- the arithmetic `get_pipelined` / `write_pipelined` form this
  replaces.
- [Fitting a timing model](./fit.md) -- the direct fit the calibration uses.
