---
title: Adding a timing model to a component
parent: Timing Models
nav_order: 2
audience: python
api: [LinCalibModel, TimingModel, FreeRunMod.add_timing_model]
summary: "How a component sources its compute delay from a timing-model object: attach a model and, in the run loop, charge self.timeout(tm.predict(...) * clk.period). tm.predict returns the cycles the compute will take as a function of the firing size (e.g. n); internally it evaluates a formula in a few parameters (e.g. latency + proc_ii*(ceil(n/U)-1)), which the following pages make concrete and the fitting section recovers by comparison to RTL. The model adds only this component's compute plus unaccounted overhead — read/write times are charged by the transfer yields, and stalls grow the firing on their own."
---

# Adding a timing model to a component

A component's **functional** model says *what* it computes; its **timing** model says *how long* that
takes. In pysim the compute is instantaneous — the value is produced at once — so a component must
**charge the time its computation would take in hardware**. It does that by holding a *timing model*
and, in its run loop, waiting the delay the model predicts.

## Where it plugs in

Attach the model in `__post_init__`; predict the delay and charge it with `self.timeout`:

```python
class MyCompute(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        # ... declare ports: s_cmd, m_mem, m_out ...
        self.tm = ...                   # a timing model — given a formula on the next pages
        self.add_timing_model(self.tm)  # register it with the component (so the fitting tools find it)

    def run_iter(self):
        cmd = yield from self.s_cmd.get(MyCmd)            # blocks until a command arrives
        x   = yield from self.m_mem.read(cmd.addr, cmd.n) # blocks on the read

        y = compute(x)                                    # the value — computed instantly in pysim

        cycles = self.tm.predict({"n": cmd.n})            # cycles the compute takes, vs. the size n
        yield self.timeout(cycles * self.clk.period)

        yield from self.m_out.write(y)                    # blocks if the downstream FIFO is full
```

`tm.predict` returns the **number of cycles** the compute will take as a function of the firing size —
here the sample count `n`. Internally it evaluates a formula in a few parameters; for a pipelined loop,
for instance,

```
cycles = latency + proc_ii · (ceil(n / unroll_factor) − 1)
```

The next pages ([Block](./block.md), [Streaming](./streaming.md)) make that formula concrete, and
[the fitting section](../calib/) shows how to recover its parameters — `latency`, `proc_ii` — by
comparison to an actual RTL simulation. (`× clk.period` converts the predicted cycles to sim time.)

## The delay is *additional*, not end-to-end

The model adds **only this component's own compute cost, plus any overhead not already charged** — not
the load or store. Those are already accounted for:

- `m_mem.read()` / `m_out.write()` block for however long the transfer actually takes, charged by the
  `yield from`.
- `s_cmd.get()` blocks until a command arrives.

So if the reads or writes **stall** — the interconnect is busy with another master, or a downstream
FIFO is full — those `yield from`s simply take longer, and the firing's end-to-end time **grows on its
own**. Congestion is the *simulation's* to model; the timing model owns only the extra compute delay.
That is why a model built in isolation still predicts the contended system.

## See also

- [Block processing](./block.md) — the next page: the formula above as a concrete `LinCalibModel`.
- [Timing model fitting](../calib/) — recovering the model's parameters by comparison to RTL.
- [Simulation timing model](../sim/timing.md) — `Clock`, `self.timeout`, and cycles → time.
