---
title: Process generators
parent: Simulation
nav_order: 2
audience: python
api: [ProcessGen, SimObj.timeout, SimObj.process, SimObj.event, SimObj.now]
summary: "A quick guide to SimPy's generator-based concurrency for readers new to it: what an event and a process generator are, how yield pauses a process, when behavior must be a generator, how to spawn a non-blocking parallel process, how to return a value (proc.value vs yield from), and how to type-hint with ProcessGen[T]."
---

# Process generators

Waveflow models concurrency with [SimPy](https://simpy.readthedocs.io/), which expresses every
time-consuming activity as a **Python generator function** — one that uses `yield`. A
[`SimObj`](./simobj.md)'s `run_proc` is exactly such a generator. If you have not used SimPy before,
the `yield` keyword in these methods can be mysterious; this page is the mental model.

## Events and process generators

Two ideas underpin everything:

- An **event** is something that *completes at a simulation time* — a timeout elapsing, another
  process finishing, an item arriving on a queue. `self.timeout(5)` and `self.event()` both return
  events.
- A **process generator** is a generator function that `yield`s events. SimPy runs it until a
  `yield`, **pauses** it there, lets simulation time advance (other processes run), and **resumes**
  it on the next line when the yielded event fires.

Its type alias is [`ProcessGen[T]`](../../../waveflow/simulation/simobj.py) — literally
`Generator[Event, Any, T]`, where `T` is the type the generator eventually `return`s (or `None`).

## `yield`: pausing on an event

`yield <event>` means *"pause this process until that event completes."* While paused, the rest of
the simulation keeps running. The classic event is a timeout:

```python
from waveflow.simulation.simobj import ProcessGen, SimObj


class Blinker(SimObj):
    def run_proc(self) -> ProcessGen[None]:
        print(f"on  at t={self.now}")
        yield self.timeout(5)          # pause 5 time units; time advances here
        print(f"off at t={self.now}")  # resumes at t=5
```

Without the `yield`, the method would run start-to-finish at `t=0` — nothing in a simulation would
take any time. The `yield` is what lets simulated time pass.

## When to use a process generator

Make a method a process generator (give it `yield`s and a `-> ProcessGen[...]` hint) whenever it
**consumes simulation time or waits on another object** — transfers, polling, handshakes, delays.
Pure computation that takes no simulated time stays an ordinary method or function — you call it
normally, no `yield`. A component's lifecycle entry (`run_proc`, or `on_start`) is always a process
generator; see [SimObj: Its lifecycle](./simobj.md#its-lifecycle).

## Sequential vs. parallel: `yield from` vs. `self.process(...)`

This is the distinction that trips up most newcomers. There are two ways to invoke another process
generator, and they mean different things:

| Form | Runs | Blocks the caller? | Returns |
|---|---|---|---|
| `result = yield from sub()` | **inline**, in the *current* process | yes — runs to completion before the next line | the sub-generator's `return` value, directly |
| `proc = self.process(sub())` | as a **separate, concurrent** process | no — starts it and returns immediately | a `Process` handle (join later with `yield proc`) |

Use `yield from` for a sub-step you want to do *now* and wait for. Use `self.process(...)` to start
work that should run **alongside** the caller.

### Spawning a non-blocking parallel process

[`self.process(gen)`](../../../waveflow/simulation/simobj.py) registers and starts a generator as a
concurrent process and hands back a handle *without* blocking. Start several, then wait for them all
with `self.env.all_of([...])`:

```python
class Fork(SimObj):
    def run_proc(self) -> ProcessGen[None]:
        a = self.process(self.work("A", 3))   # both start now and
        b = self.process(self.work("B", 5))   # run concurrently
        yield self.env.all_of([a, b])         # resume once BOTH finish
        print(f"all done at t={self.now}")     # t=5, not t=8

    def work(self, name: str, dur: float) -> ProcessGen[None]:
        yield self.timeout(dur)
        print(f"{name} done at t={self.now}")
```

Because `A` and `B` run in parallel, the join completes at `t=5` (the longer of the two), not `t=8`.

## Returning a value

A process generator may `return` a value. After it completes, SimPy stores that value on the process
handle as **`proc.value`**. Two ways to get a sub-result, matching the two forms above:

```python
class Compute(SimObj):
    def run_proc(self) -> ProcessGen[None]:
        # (1) inline — runs in this process, value comes back directly
        squared = yield from self.square(10)
        print("inline:", squared)              # 100

        # (2) concurrent — spawn, join, then read .value
        proc = self.process(self.square(12))
        yield proc                             # wait for it to finish
        print("joined:", proc.value)           # 144

    def square(self, x: int) -> ProcessGen[int]:
        yield self.timeout(1)                  # pretend it takes a cycle
        return x * x
```

The `proc.value` pattern is exactly how a memory read returns its data — see
[`MMIFMaster.read`](../../../waveflow/hw/memif.py) (`proc = self.process(...); yield proc; return proc.value`)
and the [interface overview](../interface/overview.md#simpy-integration).

### Plain events for hand-rolled signaling

For an ad-hoc rendezvous, [`self.event()`](../../../waveflow/simulation/simobj.py) makes a bare event:
one process `yield`s it; another fires it with `evt.succeed(payload)`, and the payload arrives as
`evt.value`.

```python
gate = self.event()
# consumer process:   value = yield gate
# producer process:   gate.succeed("ready")   # wakes the consumer; value == "ready"
```

## Type hinting

Annotate the return as `ProcessGen[T]`, where `T` is what the generator `return`s:

- `-> ProcessGen[None]` — a process that does work but returns nothing (most `run_proc` bodies).
- `-> ProcessGen[int]` / `ProcessGen[Words]` / `ProcessGen[bool]` — returns a value read via
  `proc.value` or `yield from` (e.g. [`aximm_queue.py`](../../../waveflow/hw/aximm_queue.py)'s
  `count() -> ProcessGen[int]`, `try_write() -> ProcessGen[bool]`).

These hints are not just documentation: for a `@synthesizable` method, the code generator reads the
annotation and **unwraps `ProcessGen[T]` to `T`** for the generated C++ return type — see
[Module Code Generation](../comp_codegen/).

## Quick reference

- A **process generator** is a function that `yield`s **events**; `yield` pauses it until the event fires.
- `yield self.timeout(d)` — let `d` time units pass; `self.now` is the current time.
- `yield from sub()` — run `sub` **inline** and get its `return` value.
- `self.process(sub())` — run `sub` **concurrently**; `yield` the handle to join, read `proc.value` for its result.
- `yield self.env.all_of([p1, p2])` — wait for several concurrent processes.
- Hint as `ProcessGen[T]` (`T = None` when nothing is returned).
