# Plan: software threads -- one host program shape, two realizations, no BFM

**Status:** drafted 2026-10-08, revised the same day around `SwThread` (the user's abstraction); Stage 0
done (2026-10-08); C++ threads are **fibers** (decided after Stage 0); Stage 1 next.  Follows `plans/xsi_system_top.md` (S1-S6, merged in PR #236), which made the host a hooked
module with a C++ twin and a per-endpoint trace gate.  This plan replaces the hand-written C++ twin's
BFM work with a **software-thread runtime** in both languages, and finishes `run_xsi(sysm)`.

## Motivation

After `xsi_system_top`, `examples/markov/markov_host.h` is 174 lines and about 30 of them are the
program; the rest is BFM work (pins, addresses, phase forwarding, reporting, the scenario, traces) and
hand-written state machines, because an XSI model is called once per cycle and cannot block.

The user's bar (2026-10-08): a C++ twin of the host is acceptable -- every hardware module already has a
Python and an HLS realization -- provided (1) the two are **verified as matched** (the trace gate does
this) and (2) software meets the bus through **clear transactional APIs in both languages**, as kernels
do through endpoints and generated serializers.  *The user should not have to write a BFM.*

## The model: software is threads; the bus is transactions

**The unit of software is a `SwThread`**: a sequential program that waits on events and on bus
transactions.  Software is not hardware, so its model is different from a kernel's:

| | Python (pysim) | C++ (XSI) |
|---|---|---|
| a thread | a **SimPy process** -- a Python generator, not an OS thread | a **fiber** -- its own stack, switched to cooperatively (below) |
| code between waits | runs in **zero simulated time** | runs between two clock edges -- zero cycles |
| waiting on an event (an interrupt, a lock, a message) | a SimPy event: `yield self.irq.wait()` | blocks the thread: `irq.wait()` |
| a bus transaction | an `MMIFMaster` transaction, timed by the bus model | an `AxiMmMaster` transaction, timed by the RTL |
| software execution time | `yield self.compute(cycles)` -- an explicit timer | `compute(cycles)` -- the same timer |

So the simple Markov host becomes **one thread** that issues commands and pends on interrupts -- a
driver, not two processes standing in for one:

```python
class MarkovHost(SwHost):                         # owns the bus master and the interrupt inputs
    def main(self):                               # the one SwThread
        sent = done = 0
        while done < len(self.jobs):
            while sent < len(self.jobs) and sent - done < MAX_IN_FLIGHT:
                yield from self.qcmd.write(self.cmd(sent)); sent += 1   # 2 x 3 words: always fits
            yield self.qresp.data_irq(1).wait()   # pend: a response is ready
            resp = yield from self.qresp.pop(MkvResp)
            x = yield from self.bus.read(self.xaddr(resp.tx_id), self.xwords(resp.tx_id))
            self.record(resp, x); done += 1
            yield self.compute(HOST_HANDLER_CYCLES)   # optional: what the handler costs
```

```cpp
void main() override {                            // MarkovHost's C++ twin -- the same lines
    int sent = 0, done = 0;
    while (done < njobs()) {
        while (sent < njobs() && sent - done < MAX_IN_FLIGHT) { qcmd.write(cmd(sent)); ++sent; }
        qresp.data_irq(1).wait();
        MkvResp resp = qresp.pop<MkvResp>();
        auto x = bus.read(xaddr(resp.tx_id), xwords(resp.tx_id));
        record(resp, x); ++done;
        compute(HOST_HANDLER_CYCLES);
    }
}
```

### The API: one Python spelling, one C++ spelling, per primitive

The core of the plan is this table: each software primitive has a Python representation (SimPy) and a
C++ equivalent with the same name and meaning.  New channels -- locks, software message queues -- are
added by adding a row: a Python class over SimPy events, and its C++ class over the runtime.

| primitive | Python (pysim) | C++ (XSI runtime) | waits for |
|---|---|---|---|
| interrupt line in | `yield irq.wait()` (level: returns at once if high) | `irq.wait()` | the line high |
| any of several | `yield wait_any(a, b, ...)` | `wait_any(a, b, ...)` | the first to fire |
| software time | `yield self.compute(cycles)` | `compute(cycles)` | the timer |
| event between threads | `ev = SwEvent(); yield ev.wait(); ev.set()` | `SwEvent` | `set()` |
| lock | `SwLock`: `yield lk.acquire()`, `lk.release()` | `SwLock` | the lock free |
| counting semaphore | `SwSemaphore` | `SwSemaphore` | a count > 0 |
| software message queue | `SwQueue`: `yield q.put(m)`, `m = yield q.get()` | `SwQueue<T>` | room / a message |
| bus: queue-in view | `yield from qcmd.write(words or schema)` | `qcmd.write(...)` | the bus writes done |
| bus: queue-out view | `qresp.data_irq(n)`; `yield from qresp.pop(T or n)` | same | the bus reads done |
| bus: register bank | `yield from regs.commit(cfg)`; `yield from regs.status(T)` | same | the bus ops done |
| bus: plain memory | `yield from bus.read(addr, n)` / `write(addr, words)` | same | the transaction done |

**Bus calls block only for the bus transaction, never for a condition.**  Waiting for data or room is
the thread's business, and it is spelled out (`data_irq(n).wait()`, `room_irq(n).wait()`), because a
single thread waiting on two queues has to choose -- that is what `wait_any` is for.  The view's
threshold register is the framework's: `data_irq(n)` sets it to `n` (a bus write, only when it changes)
and returns the interrupt, so the thread never writes a threshold or reads a count.  The current
blocking endpoints (`write` / `get_schema` that sleep inside) stay, rebuilt on these primitives, as the
convenience for a thread that waits on one thing only.

**Interrupts are level-sensitive** (`IrqIF`): `wait()` returns at once while the line is high, so a
handler must clear the cause (drain the queue, or raise the threshold) before waiting again, or it spins
-- in pysim, forever at one instant.  The runtime detects a thread that waits on an already-high line
twice with no bus operation in between and fails loudly.

### Where threads live: `SwHost`

A `SwHost` (an `HwModule` outside the cut, replacing today's host base) owns what the software has
physically: **one bus master and its interrupt inputs**.  Its threads share them.  `main()` is the
first thread; more are started with `self.start(thread_fn)` (C++: `start(&T::fn)`).  Endpoints are the
host's attributes, set by the system wiring as today.

**Execution time** is explicit (`compute`) and, to start, per thread: two threads computing at once
overlap, as on a multi-core host.  A single-core contention model (a CPU resource the timers draw on)
is a later refinement, and does not change the API.

### How C++ threads run under XSI: fibers, one at a time

A **fiber** is a thread the program schedules itself: it has its own stack (its locals, its call chain,
the point where it stopped), but it runs only when some code explicitly switches to it -- a switch saves
the CPU registers, stack pointer included, and loads the other fiber's, with no OS involvement.  That is
the C++ form of a SimPy process (a generator is a fiber whose switch points are its `yield`s), and the
same idea as stackful coroutines or green threads.  Windows provides them (`CreateFiber` /
`SwitchToFiber`); Linux has `ucontext` (`makecontext` / `swapcontext`).  The runtime hides both behind
one small `Fiber` type.

Each `SwThread` is a fiber.  The XSI cycle loop runs on the main fiber; every blocking call
(`irq.wait()`, `qcmd.write(...)`, `compute(n)`) records what the thread waits for and switches back to
the loop; in each `update()` the scheduler checks every parked thread's wait (a few ns each, no switch)
and switches, in a fixed order, into every thread whose wait completed, which runs until its next
blocking call.  This is SimPy's discipline, so the run is deterministic **by construction** -- no OS
scheduler exists to reorder anything -- and the trace gate compares like with like.  A wait already
satisfied when called (an interrupt already high) returns without a switch.

**Rule: only the loop fiber calls the XSI API.**  A thread's bus call only queues an operation on the
framework endpoint; the endpoint's state machine (`MmQueueWriter::step` ...) does the cycle work from
the loop.  Where the thread resumes is the timing contract: a thread's next transaction is issued in the
cycle its previous one completed -- exactly what today's hand-written state machines do.

**Why fibers and not OS threads** (measured in Stage 0): a switch costs **~0.05-0.2 us** against
**~12 us** for a thread hand-off and **~1.2-1.7 us** for a SimPy resumption; they build on Vivado's
**GCC 6.2** as well as 9.5, so `run.bat`'s toolchain does not change; and determinism is structural
rather than the result of careful baton passing.  What they give up -- running on another core, calling
blocking OS APIs from a host thread -- nothing in this plan needs.

### What is generated, and what the user writes

- **The user writes** the Python `SwHost` subclass (its threads) and the C++ class of the same shape in a
  `.h` beside it -- only thread bodies.
- **Framework (C++, `waveflow/build/xsi/xsi_sw.h`, new):** `SwThread`, the scheduler, `wait_any`,
  `compute`, `SwEvent` / `SwLock` / `SwSemaphore` / `SwQueue`, the bus endpoints with blocking calls
  (on `xsi_mm_host.h`), and `SwHostModel`: the `XsiSimObj` owning the bus master, the pins, the threads,
  the scenario, the cycle count, the report and the traces.
- **Generated per host (`<host>_endpoints.h`):** one member per Python endpoint, named as in Python,
  bound to its view, address and interrupt pin from the system wiring; the scenario item type.
- **Host-side schemas:** `qresp.pop<MkvResp>()` returns a struct.  Stage 0 checks whether Vitis's
  header-only `ap_int.h` compiles under the XSI toolchain; if not, `DataSchema` gains a plain-C++ flavor.
- **`bfm_model()` of a host** is derived: `SwHost` supplies it from the endpoints and the header name.

### `run_xsi(sysm)`

One framework call replaces each example's `*_xsi.py`: default cut (kernels + on-chip memories, the
`SwHost` outside); build if stale, derived from the system (each kernel declares the schemas and
element types its HLS body includes); crossbar IP, top, harness, scenario, run; decode the traces into
`host.results` (a host method, the same shape pysim produces); and the per-endpoint trace gate.

## Stages

0. **Feasibility, no framework code.**  (a) Build mm_fir's XSI testbench with GCC 9.5 -- gate numbers
   unchanged; (b) a 50-line cooperative scheduler with two OS threads under that toolchain,
   deterministic over 100 runs; (c) compile a generated schema header with `ap_int.h` there.  **Stop and
   report** if (a) or (b) fails.  *(Done; it also measured fibers, which replaced threads.)*
1. **Python `SwThread` / `SwHost`.**  The primitives in the table over SimPy, the bus calls that block only
   for the transaction, `data_irq` / `room_irq`, `wait_any`, `compute`; the existing endpoints rebuilt on
   them.  Port `FirHost` and `MarkovHost` **keeping their current process structure** (two threads each).
   Gate: all pysim tests unchanged, and pysim cycle counts identical (635 / 635, 1926).
2. **C++ runtime, mm_fir.**  `xsi_sw.h` (the `Fiber` type with its Windows and `ucontext` backends,
   the scheduler, the primitives), the generated `<host>_endpoints.h`, `FirHost`'s C++ twin as thread
   bodies.  First a unit test of the scheduler alone, compiled with **both** MinGW 6.2 and 9.5 (and the
   system `g++` on Linux, where available): a fixed interleaving log, identical every run.  **Gate: 618 / 611 unchanged, bit-exact, trace gate passes** -- same host program, so
   the bus behaviour must not move; a count that moves is a scheduling difference to find.
3. **Host-side schemas** -- `pop<T>()` / `status<T>()`.  Gate: a round-trip test per schema the hosts read.
4. **markov, two threads then one.**  First the two-thread port (gate: 1870, traces identical).  Then the
   **single-thread driver** above -- a different program, so its bus-op order and cycle count may
   differ: record the new count with the reason (probes), and only with the user's agreement replace
   `EXPECTED_CYCLES`; the trace gate (per endpoint) must still pass between its Python and C++ forms.
5. **`run_xsi(sysm)`.**  Gate: full `pytest -m xsi`, 0 skipped; each example's RTL gate is
   `run_xsi(System(...))`, and `*_xsi.py` keeps only its scenario and probes, if anything.
6. **Channels between threads.**  `SwLock`, `SwQueue` exercised by a small two-thread host (e.g. a
   producer thread and a consumer thread sharing a software queue in front of the hardware queue).
   Gate: Python and C++ traces identical.
7. **Docs.**  A guide page for software threads (the table above, side by side); `xsi_system.md`;
   `bfm_model.md` (a host no longer writes a model); the markov pages.  And a **blind test**: a fresh agent
   writes the host for a new small system from the docs alone.

## Progress log

### Stage 0 -- feasibility: DONE, all three pass (2026-10-08)

Prototypes lived in a scratch directory; no framework code changed.

- **(a) GCC 9.5 runs the XSI flow unchanged.**  A copy of `run.bat` with `MINGW` pointed at
  `tps/mingw/10.0.0` (GCC 9.5.0, `x86_64-msvcrt-posix-seh`), run against copies of the real
  workspaces: mm_fir (`per_view`) **618** and markov **1870**, and the **complete testbench output is
  identical** to the 6.2 build (68 and 19 lines -- every bus operation with its cycle stamps).  The
  binary was confirmed as a 9.5 build (its embedded `GCC:` string); it loads 9.5's `libstdc++-6.dll` /
  `libgcc_s_seh-1.dll` from `PATH` beside `xsimk.dll` with no clash.  GCC 6.2 cannot even compile
  `<condition_variable>` threading (`unique_lock` undeclared), so the switch is required, not optional.
- **(b) A cooperative scheduler over OS threads is deterministic.**  ~100 lines: the cycle loop and each
  thread hand a baton back and forth (only one runs at a time); each cycle the loop resumes, in a fixed
  order, every thread whose wait completed.  Two threads interacting through timed waits and an event
  produce the **same log on 100 of 100 runs**, in the order SimPy would.  **Cost: ~12 us per hand-off**
  steady state (20,000 hand-offs, one condition variable *per party* with `notify_one`); a single shared
  condition variable with `notify_all` was 4-5x slower.  A hand-off happens per bus operation or event,
  not per cycle, so it is about the cost of simulating one RTL cycle -- negligible for markov's 14 bus
  operations, ~1 s per 10^5.
- **(c) The generated HLS schema headers work in host code -- no plain-C++ flavor needed.**  `mkv_resp.h`
  compiled under GCC 9.5 with `-I Vitis/include` (Vitis's header-only `ap_int.h`, `hls_stream.h`), with
  no errors or warnings, in one translation unit with `xsi_bfm.h` / `xsi_mm_host.h`; `read_array<64>` on
  the two words Python serialized for `MkvResp(n=300, ones=257, tx_id=2)` returned exactly those fields.
  Compile time unchanged (~5 s, same as today's `markov_tb.cpp`).  So Stage 3 reuses the existing
  structs; the host build needs the Vitis include path (found by `waveflow/toolchain`).

**Then: fibers instead of threads.**  The same baton-passing scheduler written on Windows fibers
(`CreateFiber` / `SwitchToFiber`), same 20,000-hand-off benchmark: **~0.05-0.2 us per hand-off** -- about
100x cheaper than the OS threads and about 10x cheaper than SimPy itself (measured on the same machine:
**~1.2 us** per resumption of a `timeout`, **~1.7 us** for a fresh event created, triggered and
resumed).  It builds and runs under **GCC 6.2 and 9.5 alike**.  Decided with the user (2026-10-08): C++
`SwThread`s are fibers.

**Consequences for the next stages.**  `run.bat` keeps GCC 6.2 -- no toolchain switch, so no
re-validation of the 161 XSI gates on a new compiler (finding (a) stays as evidence that 9.5 would also
work).  The scheduler's shape is settled: one fiber per `SwThread`, the cycle loop on the main fiber,
resume in registration order, only the loop calls XSI.  Linux needs the `ucontext` backend -- not
testable on this machine; Stage 2 builds it and the Linux CI (if any) or a later Linux run checks it.
Finding (c) is unaffected: host-side schemas use the existing generated headers.

## Non-goals

- **Driving XSI from the Python host** -- deferred by the user (2026-10-08): two realizations per module
  is the model; the runtime does not preclude it later.
- **Generating the C++ from Python** -- still rejected; the trace gate ties the two together.
- Preemptive scheduling, priorities, a real OS model, OS threads.  Threads are cooperative fibers;
  time is explicit.
- AXI-Lite / `HostActivated` DUTs, RF converters, several crossbars.

## Open questions

- **Units of `compute`.**  Host clock cycles (proposed: the system clock, as the bus master's) or
  nanoseconds with a host clock declared on `SwHost`.
- **Single-core contention.**  When two threads `compute` at once -- overlap (now) or serialize on one
  CPU (later, a resource).
- **Interrupt acknowledge.**  Level-sensitive is what the adaptors drive; an edge / ack-register
  interrupt (a real IRQ controller) would be a new primitive row, not a change.
- **Results decoding** -- a host method `results_from_traces` beside its scenario layout.

## Related

- `plans/xsi_system_top.md` -- the generated system top, the hooked host, the trace gate (done).
- `plans/xrt_target.md` -- Stage 5's pyxrt backend is a third host realization; its calls should map onto
  the same primitives (`run.wait()` is an event wait).
- `waveflow/hw/irq.py` (`IrqIF`), `waveflow/hw/mm_host.py` (the endpoints), `waveflow/build/xsi/xsi_mm_host.h`.
