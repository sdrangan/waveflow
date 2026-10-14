---
title: Pipelined bodies
parent: Timing Models
nav_order: 4
audience: python
snippets: run
api: [TimedPhase.pipeline, PipelineResult, StreamIFSlave.req, LoadRequest]
summary: "A pipelined body -- the loop that reads a word, computes, writes a word -- overlaps its input and output: the first output comes a latency after the first input, and later outputs are paced by the slower of input arrival and the loop's rate. Declared as two spans, latency (input first -> output first) and fill_time (output first -> output last), and run with `phase.pipeline(fn, load=ep.req(...), store=ep, **features)`, which waits exactly that race from real events and returns what it did. Run on a small module: 8 words, latency 5, fill 7."
---

# Pipelined bodies

A **pipelined** body does not wait for its whole input: the loop reads a word, computes, writes a word,
so the output starts while the input is still arriving.  That overlap is what makes it the harder shape
to time -- and what `phase.pipeline` does for you.

## The race

Let `L` be the loop's pipeline depth (its **latency**), `II` its initiation interval, and `m` the words.
Output word `k` cannot leave before input word `k` arrived and went through the pipeline, nor sooner
than `II` after the output before it:

```
t_out[k]    = max( t_in[k] + L ,  t_out[k-1] + II )

t_out_first = t_in_first + L
t_out_last  = max( t_in_last + L ,  t_out_first + II·(m − 1) )
                  └ input-limited ┘  └──── compute-limited ────┘
```

The output is paced by whichever is slower.  Collapsed to the first and last words -- the only two
events a [loosely-timed](./models.md) transfer has -- that is two spans:

- **`latency`**: from the input's **first** beat to the output's **first** beat (`L`);
- **`fill_time`**: from the output's **first** beat to its **last** (`II·(m − 1)`, the
  [loop model](./loops.md)'s second term),

and a tail no span has to state: the last output word cannot leave before the last input word plus `L`.

## The pattern

Declare the two spans, and in the body hand the phase the compute, what to load and where to store:

```python
from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.timed_phase import TimedPhase
from waveflow.simulation.simulation import Simulation

N = 8


@dataclass
class Double(FreeRunMod):
    clk: Clock = field(default_factory=lambda: Clock(freq=1.0))   # 1 Hz: times read as cycles

    def __post_init__(self):
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=64)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=64)
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)
        self.loop_phase = TimedPhase(
            "loop", spans={"latency": (self.s_in.first, self.m_out.first),
                           "fill_time": (self.m_out.first, self.m_out.last)},
            features=("nwords",),
            seed={"latency": {"intercept": 5.0, "nwords": 0.0},           # L = 5
                  "fill_time": {"intercept": -1.0, "nwords": 1.0}})       # II·(m − 1), II = 1
        self.add_timing_phase(self.loop_phase)
        self.results = []

    def run_iter(self):
        r = yield from self.loop_phase.pipeline(lambda w: w * 2, load=self.s_in.req(nwords_max=N),
                                                store=self.m_out, nwords=N)
        self.results.append(r)
```

- **`load=self.s_in.req(...)`** is a *request*, not a read: `pipeline` drives the transfer's two
  halves itself.  `req()` takes the next burst's raw words, `req(nwords_max=N)` at most `N` of them,
  `req(T, count)` typed elements as `get_array` returns them.
- **`store=self.m_out`** is the endpoint the result goes out on.
- **The call is checked against the declaration**: the load must be on the endpoint whose `first`
  starts `latency`, the store on the one that ends it and carries `fill_time`.
- **`pipeline` is a `yield from`**: a sequence of waits, not one event.

## Running it

```python
def run(bursts):
    sim = Simulation()
    env = sim.env
    dut = Double(name="dbl", sim=sim)
    drv = StreamIFMaster(name="drv", sim=sim, bitwidth=64)
    snk = StreamIFSlave(name="snk", sim=sim, bitwidth=64, queue_size=64)
    for name, master, slave in (("a", drv, dut.s_in), ("b", dut.m_out, snk)):
        link = StreamIF(name=name, sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=16)
        link.bind("master", master)
        link.bind("slave", slave)
    out = []

    def driver():
        for b in bursts:
            yield from drv.write(np.asarray(b, dtype=np.uint64))

    def sink():
        for _ in bursts:
            w = yield from snk.get()
            out.append((env.now, [int(v) for v in w]))

    env.process(driver())
    env.process(sink())
    env.process(dut._run_iter_forever())
    env.run(until=200)
    return dut, out


dut, out = run([list(range(8))])
print(out)
r = dut.results[0]
print(r.load.first, r.load.last, r.store.first, r.store.last)
for rec in dut.loop_phase.records:
    print(rec.span, rec.predicted, rec.actual)
```

```text
[(13.0, [0, 2, 4, 6, 8, 10, 12, 14])]
1.0 8.0 6.0 13.0
latency 5.0 5.0
fill_time 7.0 7.0
```

The input's beats run 1 .. 8 and the output's 6 .. 13: the results start `latency = 5` after the first
input word, **while the input is still arriving** -- overlap, not a sequence -- and fill in
`II·(m − 1) = 7`.

## What `pipeline` does

Every wait is a real event plus a prediction; nothing is computed backwards from a later time:

```
L.first            the words are in hand; y = fn(words)
L.first + latency  the store begins -- its first beat lands there (S.first)
L.last             the load ends when its last word was read: delivered, and a word a cycle after
                   the first, so a burst that waited in the buffer is still read at the loop's rate
max(S.first + fill_time, L.last + latency)
                   the fill, and the tail
S.last             the store is delivered, waiting for room if the reader is behind
```

That is the race above, from events: the first output is input-limited, the last the later of the
input-limited and compute-limited bounds.  Because the store holds its wire from `S.first` to `S.last`,
a shared bus is held when it is really used.

## What it returns

`pipeline` returns a `PipelineResult`: `r.y` is the compute's result (what was written), and `r.load`
and `r.store` are the two transfers -- how many words arrived, when, and whether the other side held
them up.  A body that decides from its input reads `r.load`, so the compute stays a pure function.
Poly halts when a sample burst ends early, at TLAST:

<!-- snippet: skip -->
```python
r = yield from self.samp_phase.pipeline(compute, load=self.s_in.req(nwords_max=nwords_max),
                                        store=self.m_out, nwords=nwords_max)
if r.load.nwords < nwords_max:      # TLAST came early: halt, read nothing more
    ...
```

Such an occurrence is marked **short** on its records -- the features describe the request, not what
moved -- and a [calibration](./fit.md) leaves it out:

```python
dut, out = run([list(range(5))])
print(dut.results[0].load.nwords, [rec.short for rec in dut.loop_phase.records])
```

```text
5 [True, True]
```

## Streaming versus block is one event

A [sequential body](./block.md) is the same idea with the span starting at the input's **last** beat:
the compute waits for the whole input.  A pipelined one starts at the **first**.  Which one a module is
is a fact about its HLS body -- a straight-line loop per message is pipelined
(`docs/guide/vectorization/hls/loop_optimization.md`).

## See also

- [What a timed phase is](./timed_phases.md) -- poly's `latency` and `fill_time` on a real trace.
- [The form of a span's model](./loops.md) -- why `fill_time` is `II·(m − 1)`.
- [Calibrating a timed phase](./fit.md) -- fitting both spans from an RTL trace.
