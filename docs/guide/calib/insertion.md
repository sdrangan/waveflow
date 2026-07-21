---
title: Adding a timing model to a component
parent: Calibration
nav_order: 1
audience: python
api: [TimingModel, FreeRunComp, FreeRunComp.add_timing_model, FreeRunComp.timed_delay]
summary: "Where a timing model plugs into a component, usage-first — before any fitting. A FreeRunComp attaches a TimingModel in __post_init__ and, in run_iter, charges the delay it predicts with self.timeout. The model adds only this component's OWN compute/control cost; waiting on inputs and downstream backpressure emerge from the simulation itself, so a model fit in isolation still predicts the contended system. timed_delay is the one-call idiom that predicts AND records the firing for later fitting; an unfitted model predicts 0, so attaching one is a no-op until calibrated."
---

# Adding a timing model to a component

The [forward timing models](../timing_model/) predict a component's timeline from a few numbers —
`latency`, `ii`, a per-transfer cost. On those pages the numbers are set by hand. This page is the
other half: how a component **sources that delay from a model object** attached to it, so the numbers
can later be [fit from measurement](./component_residual.md) instead of transcribed. We show the usage
first — where it plugs in and what it does — before any fitting.

## Where it plugs in

A `FreeRunComp` runs a `run_iter` loop: read inputs, compute, write outputs. In pysim the *compute*
takes no simulated time — the value is produced instantly — so the component must **charge the time
that computation would take in hardware**. A timing model turns a description of the firing (how many
words, how many bursts) into that delay.

It attaches in `__post_init__` and is called in `run_iter`:

```python
from waveflow.calib.timing_model import TimingModel
from waveflow.hw.hw_freerun import FreeRunComp

class MyCompute(FreeRunComp):
    def __post_init__(self):
        super().__post_init__()
        # ... declare ports: s_cmd, m_mem, m_out ...

        # attach a timing model — features describe a firing, the target is the added delay
        self.tm = TimingModel(component="my_compute", calib_dir=self.calib_dir,
                              features=["nwords"], clk=self.clk)
        self.add_timing_model(self.tm)

    def run_iter(self):
        cmd = yield from self.s_cmd.get(MyCmd)     # blocks until a command arrives
        x   = yield from self.m_mem.read(cmd.addr, cmd.n)   # blocks on the read

        y = compute(x)                             # the value — computed instantly in pysim

        # charge the time the compute would take in hardware
        yield self.timeout(self.timed_delay({"nwords": cmd.n}))

        yield from self.m_out.write(y)             # blocks if the downstream FIFO is full
```

## What the delay is — and what it is *not*

This is the point that makes the whole approach work: the model adds **only this component's own
compute/control cost**. It does **not** model waiting on inputs or downstream backpressure — those
**emerge from the simulation itself**:

- `s_cmd.get()` blocks until a command actually arrives.
- `m_mem.read()` blocks for the [bus transfer](./bus_model.md) and any contention on the shared memory.
- `m_out.write()` blocks when the downstream FIFO is full.

So a firing that stalls waiting on the read or a full output is *the simulation* modeling congestion,
not the timing model. The model's job is just the extra delay of *this* component's work. That is why a
model calibrated in isolation still predicts the **contended** system — the contention is simulated, not
baked into the model — and why fitting uses only *uncontended* firings
([`blocked == 0`](./component_residual.md#collect-join-fit)).

## `predict` vs `timed_delay`

There are two ways to call it, and the difference is *recording*:

```python
# explicit: predict the delay and charge it
dly = self.tm.predict({"nwords": cmd.n})[0]
yield self.timeout(dly)

# the idiom: predict AND record this firing (features + the delay) for later fitting
yield self.timeout(self.timed_delay({"nwords": cmd.n}))
```

`timed_delay` is the one you want: it predicts through the attached model *and* appends the firing to
`self.firing_records`, which is the corpus a [fitting](./component_residual.md) run collects. `predict`
alone just returns the number. Call `timed_delay` unconditionally — with no model attached it returns
`0.0` and records nothing, so it is a no-op on an uncalibrated component.

## Cycles vs. time

Internally the model works in **cycles** — that is what keeps its fitted parameters clock-independent
(see [the index](./index.md#everything-is-in-cycles)). Because it carries `clk`, `predict` /
`timed_delay` convert to *time* for you, so the result drops straight into `self.timeout`. (Without a
`clk` the model returns cycles, and you scale by `self.clk.period` yourself.)

## Before it is fit

Attaching a model does **not** require a fit. An unfitted model predicts from its seed — zero added
delay — so the component simulates exactly as before, while `timed_delay` quietly records each firing.
That is deliberate: you attach the model, run the sim to *collect* firings, then
[fit](./component_residual.md) — and only then does `predict` start returning a non-zero delay. The
mem-streams (`MemRStream` / `MemWStream`) carry exactly this hook, inert until calibrated.

## See also

- [Component residuals](./component_residual.md) — the next step: fitting the model you just attached.
- [Timing Models](../timing_model/) — the forward-model shapes (block / streaming / double-buffered)
  whose delays this sources from a fitted model.
- [Simulation timing model](../sim/timing.md) — `Clock`, `self.timeout`, and where compute vs. transfer
  latency is charged.
