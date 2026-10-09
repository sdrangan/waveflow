---
title: Software threads
parent: Build System
nav_order: 6.6
audience: python
summary: "The host side of a system as software threads, with one API in Python and C++. A SwHost owns a bus master and interrupt inputs and runs SwThreads; in pysim a thread is a SimPy process (code between waits takes no time), and its C++ twin under XSI is a fiber, scheduled cooperatively so the run is deterministic. The primitive table side by side (interrupt waits, wait_any, compute, SwEvent / SwSemaphore / SwLock / SwQueue, the bus calls), how a C++ host is written (thread bodies on a generated <Host>_endpoints base -- no BFM, no addresses, no state machine), typed messages, settings as DynParams, the scheduler's ordering rules, and the trace gate that checks the two realizations agree."
---

# Software threads

Hardware modules are clocked and synthesized. The **host** -- the program that drives a system over
its bus -- is software, and it is modeled as **threads**: sequential code that waits on events and on
bus transactions. Its code between waits takes no simulated time unless it says so.

A host has two realizations, like a kernel:

| | Python (pysim) | C++ (XSI) |
|---|---|---|
| the host | a `SwHost` ([`waveflow/sw`](../../../waveflow/sw/threads.py)) | a class deriving from the generated `<Host>_endpoints` |
| a thread | a **SimPy process** -- a Python generator | a **fiber** -- its own stack, switched to cooperatively |
| code between waits | zero simulated time | zero cycles |
| a wait | `yield from x.wait()` -- a SimPy event | `x.wait()` -- the thread parks |
| a bus transaction | timed by the pysim bus model | timed by the RTL |

The two are written line for line alike, run the same **scenario file**, and are checked against each
other by comparing every endpoint's **trace** byte for byte -- see
[the gate](#the-gate-the-two-realizations-agree). Worked examples: `FirHost` in
[`examples/mm_fir/mm_fir.py`](../../../examples/mm_fir/mm_fir.py) with
[`mm_fir_host.h`](../../../examples/mm_fir/mm_fir_host.h), and `MarkovHost` in
[`examples/markov/markov.py`](../../../examples/markov/markov.py) with
[`markov_host.h`](../../../examples/markov/markov_host.h) -- each a producer thread and a consumer
thread ([The host](../../examples/markov/host.md) walks through it).

## The primitives, side by side

| primitive | Python | C++ | waits for |
|---|---|---|---|
| interrupt | `yield from irq.wait()` | `irq.wait()` | the line high (at once if it is) |
| any of several | `k = yield from wait_any(a, b)` | `k = wait_any(sched_, {...})` | the first ready; its index |
| software time | `yield from self.compute(n)` | `compute(n)` | *n* host clock cycles |
| another thread | `t = self.start(fn)`; `yield from t.join()` | `start("name", [this] { fn(); })` | -- |
| event | `SwEvent(self)`: `set()`, `yield from ev.wait()` | `SwEvent`: `set()`, `wait()` | set |
| semaphore | `SwSemaphore(self, n)`: `yield from acquire()`, `release()` | `SwSemaphore` | a count |
| lock | `SwLock(self)` | `SwLock` | free |
| message queue | `SwQueue(self, cap)`: `yield from put(m)`, `m = yield from get()` | `SwQueue<T>` | room / a message |
| queue in (bus) | `yield from q.write(words or schema)` | `q.write(words)`, `q.write(T)` | room (interrupt), then the bus writes |
| | `irq = yield from q.room_irq(n)`; `yield from q.push(...)` | `q.room_irq(n)`; `q.push(...)` | the bus only |
| queue out (bus) | `yield from q.get_schema(T)` / `get_array` / `get(nwords_max=n)` | `q.get<T>()`, `q.get(n)` | the data (interrupt), then the bus reads |
| | `irq = yield from q.data_irq(n)`; `yield from q.pop(T or n)` | `q.data_irq(n)`; `q.pop<T>()` / `pop(n)` | the bus only |
| register bank | `yield from cfg.write(c)`; `yield from status.read()` | `cfg.write(...)`; `status.read<T>()` | the bus |
| plain memory | `BusReader(m)`: `yield from rd.read(n, addr)` / `write` | `BusRw`: `read(addr, n)` / `write` | the transaction |

Two kinds of bus call. The plain ones (`write`, `get_*`) **wait for the condition** -- room, or data -- on
the view's interrupt, then move the words: the right call for a thread that waits on one thing. The
`room_irq` / `push` and `data_irq` / `pop` pairs **block only for the bus transaction**: arm the
interrupt (the framework writes the threshold), wait on it -- perhaps with `wait_any` among others --
then move the words. Neither ever reads a queue's count, so a host built from either never polls.

**Interrupts are level-sensitive.** `irq.wait()` returns at once while the line is high, so a handler
must clear the cause (drain the queue, re-arm the threshold) before it waits again, or it spins -- in
pysim, forever at one instant. `IrqIFSink.wait()` stops such a thread with that diagnosis.

## Writing a host

**Python.** Subclass `SwHost`. Create what the software physically has, and write `main()`:

```python
@dataclass
class MarkovHost(SwHost):
    jobs: list = field(default_factory=list)
    max_in_flight: DynParam[int] = MAX_IN_FLIGHT        # a setting the C++ twin reads too
    cpp_model: ClassVar[str | None] = "MarkovHostModel"
    cpp_header: ClassVar[str | None] = "markov_host.h"  # beside this file

    def __post_init__(self) -> None:
        super().__post_init__()
        self.add_bus_master("m", bitwidth=DW)           # registered: the C++ twin binds to it
        self.irq = {v: self.add_irq(f"irq_{v}") for v in ("qcmd", "qresp")}
        self.qcmd = self.qresp = None                   # the system wiring sets these
        self.mem_reader = BusReader(self.m)
        self.slots = SwSemaphore(self, int(self.max_in_flight), name="slots")

    def scenario_bursts(self):                          # the jobs, as the words both realizations send
        ...

    def main(self):
        self.start(self._writer)                        # a second thread
        yield from self._reader()                       # this thread
```

The program's endpoints (`qcmd`, `qresp`) are left for the system to wire -- over the bus or directly --
so the same host runs both ways ([The system](../../examples/markov/system.md)).

**C++.** Derive from the generated `<Host>_endpoints` and override `main()`; each Python thread becomes a
function on the same endpoints:

```cpp
#include "MarkovHost_endpoints.h"
#include "mkv_resp.h"
#include "xsi_sw_schema.h"

class MarkovHostModel : public MarkovHost_endpoints {
public:
    using MarkovHost_endpoints::MarkovHost_endpoints;
    long max_in_flight = 0;                              // DynParam, assigned by the harness
    void main() override {
        decode();                                        // scenario_bursts() -> jobs
        slots_.reset(new SwSemaphore(sched_, max_in_flight));
        start("writer", [this] { writer(); });
        reader();
    }
    ...
};
```

What the framework supplies, so the C++ holds only the program:

- **`<Host>_endpoints.h`, generated from the wired host** ([`sw_host_gen.py`](../../../waveflow/build/sw_host_gen.py)):
  every endpoint named as in Python, constructed on its view at its absolute bus address with its
  interrupt, the bus master, and the constructor the harness calls. No address, pin or threshold is
  written by hand.
- **The runtime** ([`xsi_sw.h`](../../../waveflow/build/xsi/xsi_sw.h), [`xsi_fiber.h`](../../../waveflow/build/xsi/xsi_fiber.h)):
  the blocking calls, the channels, the scheduler, the scenario, the cycle count, the `DONE` / `OP`
  report and the traces. `report()` may add lines.
- **Typed messages** ([`xsi_sw_schema.h`](../../../waveflow/build/xsi/xsi_sw_schema.h)): the generated
  DataSchema structs work in host code -- `qresp.get<MkvResp>()`, `r.tx_id` -- so a field is read by name.
- **Settings and files as DynParams**: `SwHost` carries `scenario` (the bundle `write_scenario` writes, so
  no job is restated in C++) and `trace_dir`; any other setting (`max_in_flight`) is a `DynParam` on the
  host, assigned to the C++ member of the same name before the threads start.
- **`bfm_model()`** is derived from `cpp_model` / `cpp_header` and the host's bus master and interrupt
  inputs; `check(host, "xsi_bfm_model")` verifies the class exists in that header.

Run it on a build DAG with [`add_system_steps`](xsi_system.md#running-it) -- or in one call, `run_system_xsi`.

## How the C++ threads are scheduled

Each thread is a **fiber** -- a thread the program switches to itself, with no OS scheduler (Windows
fibers; `ucontext` on Linux). Only one runs at a time, so the run is deterministic. Once per clock
cycle the scheduler:

1. walks the threads **in start order** (`main` first, then each in the order it was started), and for
   each steps the endpoints that thread uses -- their state machines do the cycle's bus work -- then runs
   the thread if its wait is satisfied, until its next blocking call;
2. **settles**: runs again every thread whose wait became satisfied during this cycle (a slot released,
   a message queued, an event set), until none is -- so a software event wakes its waiter in the same
   cycle, as a SimPy event wakes its waiter at the same instant.

A wait already satisfied when called returns without a switch. A switch costs about 0.1 µs, and happens
per bus operation or event, never per idle cycle -- a thread asleep on an interrupt costs nothing.
**Only the scheduler touches the simulator**: a thread's call queues work on an endpoint, and the
endpoint's state machine, stepped by the scheduler, drives the bus.

## The gate: the two realizations agree

Nothing static can show that a C++ host behaves like its Python twin, so a run checks it:

- both realizations run **one scenario file** -- the host's `scenario_bursts()`, written once by
  `write_scenario`;
- **every endpoint records what crossed it** -- each message written, each read, each region read back --
  in Python (`mm_host.py`) and in C++ (`xsi_mm_host.h`), and dumps it as a burst bundle per endpoint;
- the system DAG's [pysim](xsi_system.md#pysim) step runs the same system in pysim from the same file,
  and its [compare](xsi_system.md#compare) step compares the bundles **byte for byte, per endpoint**
  (`run.trace_mismatches`). Per endpoint, because the interleaving across
  endpoints is timing, and pysim is loosely timed. The RTL cycle count is a gate of its own.

`tests/examples/test_sw_channels_xsi.py` is the scheduler's sharpest test: `FirHost` with its writer
split into two threads through a `SwQueue` gives the same 618 cycles as `FirHost` itself.

**Source of truth:** [`waveflow/sw/threads.py`](../../../waveflow/sw/threads.py),
[`waveflow/hw/mm_host.py`](../../../waveflow/hw/mm_host.py),
[`waveflow/build/xsi/xsi_fiber.h`](../../../waveflow/build/xsi/xsi_fiber.h),
[`waveflow/build/xsi/xsi_sw.h`](../../../waveflow/build/xsi/xsi_sw.h),
[`waveflow/build/sw_host_gen.py`](../../../waveflow/build/sw_host_gen.py); the plan is
`plans/host_runtime.md`.
