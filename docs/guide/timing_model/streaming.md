---
title: Streaming processing
parent: Timing Models
nav_order: 5
audience: python
api: [StreamIFSlave.get_pipelined, StreamIFMaster.write_pipelined, SimObj.timeout, Clock.period]
summary: "The streaming timing model: compute overlaps the load, so the first output appears `latency` cycles after the first input and later outputs are gated by whichever is slower — input arrival or compute rate. The per-element max() collapses to a first/last form; only first and last arrival times are known."
---

# Streaming processing

**Streaming** is the opposite end of the continuum from [block](./block.md): instead of loading the
whole array and *then* computing, the compute pipeline starts as soon as the **first** input
element arrives and runs concurrently with the rest of the load. There is no load-then-compute
barrier — load and compute overlap element by element.

That overlap is exactly what makes streaming harder to model. In block processing the compute
window is a single `timeout` after the load finishes. In streaming, each output's time depends on
two competing pressures, and we have to take the later of them.

The two parameters are the same ones as the [loop model](./loops.md) — `latency` (pipeline depth) and
`ii` (initiation interval). The difference is only *how they are used*: a block process lumps them into
a single `latency + ii·(m − 1)` delay, while streaming needs them **separately** — `latency` sets when
the first output appears, `ii` the gap between the rest. In a fitted design both come from the model's
coefficients (`self.tm.coeffs["latency"]`, `["ii"]`); below they are written `self.latency` /
`self.proc_ii` for brevity.

## The per-element model

Let `L` be the pipeline `latency`, `II` the initiation interval, and `T` the clock period
(`self.clk.period`). For output element `k`:

```
t_out[k]    = max( t_in[k] + L*T ,  t_out[k-1] + II*T )

t_out_first = t_in_first + L*T
t_out_last  = max( t_in_last + L*T ,  t_out_first + (m-1)*II*T )
                  └─ input-limited ─┘   └──── compute-limited ────┘
```

Read the per-element line as a race between two bounds:

- **`t_in[k] + L*T` — input-limited.** Output `k` cannot exist until input `k` has arrived
  (`t_in[k]`) and traversed the pipeline (`L*T`). If the source is slow, this dominates.
- **`t_out[k-1] + II*T` — compute-limited.** The pipeline can emit at most one result every `II`
  cycles, so output `k` is at least `II*T` after output `k-1`. If the source is fast, this
  dominates.

The output is paced by whichever is slower, hence the `max`. Collapsing the recurrence to just the
**first** and **last** elements gives the form we actually compute: the first output is purely
input-limited (`t_in_first + L*T`), and the last output is the later of the input-limited bound
(`t_in_last + L*T`) and the compute-limited bound (`(m-1)` more iterations at `II` after the
first). The `(m-1)` is the same effective-trip-count convention as in
[block processing](./block.md) — `m = ceil(n / unroll_factor)`
iterations means `m-1` outputs after the first.

> A timeline figure for this page (deferred) would show the **load** bar and the **compute** bar
> overlapping, with the first output offset `L*T` into the load and the gap between outputs set by
> the slower of arrival and `II` — visually, the two bounds racing.

## The pattern

```python
import math
from waveflow.hw.arrayutils import array

# Wait for the entire vector x to load, but also retrieve when the first
# sample was loaded.  After this returns, env.now == t_in_last.
x, t_in_first = yield from s_in.get_pipelined(Float32, count=n)

y = compute(x)
m = math.ceil(n / self.unroll_factor)
T = self.clk.period

# First output is ready `latency` cycles after the first input.
t_out_first = t_in_first + self.latency * T

# Last output: gated by the later of the input-limited and compute-limited bounds.
# env.now == t_in_last, so the input-limited bound is (env.now + latency*T).
t_out_last = max(self.env.now  + self.latency * T,
                 t_out_first   + (m - 1) * self.proc_ii * T)

# Stay busy until the last output is emitted.
yield self.timeout(t_out_last - self.env.now)

# Write the output, tagging the stream with when it could start.
yield from m_out.write_pipelined(array(Float32, y), t_out_first)
```

[`get_pipelined`](../interface/stream.md) returns the data **and** the first-word arrival time:
after it returns, `self.env.now` is `t_in_last` (the whole burst has arrived), and `t_in_first` is
when the first word landed. [`write_pipelined`](../interface/stream.md) tags the outgoing stream
with `t_out_first` so the *next* component downstream sees a correctly-timed first word and can
overlap against it in turn — that is how streaming composes across a pipeline of components.

Note the division of labor between the two delays here. The **compute rate** is set by
`self.proc_ii` and is charged by the `yield self.timeout(...)` above — that is what holds the
component busy until its last result exists. The **output transport rate** is a separate concern
owned by `write_pipelined`, which models the burst as leaving at one word per clock from
`t_out_first`. Do not read `proc_ii` as the rate at which words go out on the stream: it paces the
datapath, not the wire. (In this pattern the `timeout` has already advanced past `t_out_first`, so
`write_pipelined`'s remaining delay collapses and the compute `timeout` dominates the total —
`write_pipelined` is only setting the downstream *start* tag, not adding fresh time.)

## Two simplifications to be honest about

This model is loosely timed, and it makes two deliberate approximations:

1. **Only first and last arrival times are known.** `get_pipelined` gives us `t_in_first` and
   `t_in_last`, not the full per-element arrival profile. The `max()` therefore assumes the inputs
   arrive **densely and uniformly** between those two times. A bursty source that pauses mid-stream
   would, in real hardware, stall the pipeline at points this model smooths over.
2. **Functional compute happens "all at once."** `y = compute(x)` runs as a single numpy-level
   operation on the whole vector — only the *timestamps* are pipelined, not the data. The model
   reproduces *when* each output would be available, but it does not actually push elements through
   a cycle-by-cycle datapath. (This is the LT bet from [LT vs CT](./models.md): model the
   transaction's timing, not every cycle.)

## See also

- [Block processing](./block.md) — the no-overlap baseline this is the streaming limit of.
- [Timing models for loops](./loops.md) — the `latency` / `ii` parameters this uses (separately here).
- [Adding a timing model to a component](./insertion.md) — the general pattern; streaming is the
  overlapped variant.
- [Stream Interfaces](../interface/stream.md) — `get_pipelined` / `write_pipelined` and the first-word-time mechanism.
- Double-buffering (block-granularity overlap) is built by composing sub-components over a
  [stream of blocks](../interface/sob.md), not a separate timing model.
