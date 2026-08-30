---
title: Overview
parent: Interfaces
nav_order: 1
audience: python
api: [Interface, InterfaceEndpoint, StreamIF, StreamIFMaster, StreamIFSlave, MMIFMaster, MMIFSlave, Words, Simulation]
summary: "The transactional interface model — Interface vs master/slave endpoint, the Words type, the cycle-based latency model, and the SimPy write/read/bind lifecycle, plus a runnable two-SimObj StreamIF toy."
---

# Overview

## Core concepts

### Interface

An `Interface` is a named object that connects exactly two or more **endpoints**. It owns the latency model and the routing logic for all data passing over the connection. The interface knows the clock frequency, the data bitwidth, and any protocol-specific parameters (queue depth, protocol type, address ranges, and so on).

```python
from waveflow.hw.interface import StreamIF
from waveflow.hw.clock import Clock

clk = Clock(freq=100e6)   # 100 MHz
iface = StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)
```

### InterfaceEndpoint

An `InterfaceEndpoint` is the **handle** that a component holds to participate in an interface. There are always two roles:

- **Master endpoint** — initiates transactions (calls `write`, `read`).
- **Slave endpoint** — receives transactions (provides `rx_proc` callbacks).

Endpoints are created by the component that owns them, then registered with an interface via `bind()`.

```python
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave

# Created inside each component's __post_init__
master_ep = StreamIFMaster(sim=sim, bitwidth=32)
slave_ep  = StreamIFSlave(sim=sim, bitwidth=32, rx_proc=self.on_receive)

# Wired together pre-simulation
iface.bind("master", master_ep)
iface.bind("slave",  slave_ep)
```

### Words

All interfaces transfer data as **numpy arrays of fixed-width integers**, aliased as `Words`:

```python
from waveflow.hw.interface import Words   # NDArray[uint32] | NDArray[uint64]
import numpy as np

words = np.array([0xA0, 0xA1, 0xA2], dtype=np.uint32)
```

The convention is:
- `bitwidth <= 32` → `dtype=np.uint32`
- `bitwidth <= 64` → `dtype=np.uint64`
- `bitwidth > 64`  → `(n, k)` array of `uint64` in little-endian word order

### Latency model

All interfaces model transfer time as a **cycle count divided by clock frequency**. For a transfer of `nwords` over a channel with `latency_init` setup cycles and clock frequency `clk.freq`:

```
transfer_time = (latency_init + nwords) / clk.freq   [seconds]
```

The `latency_init` captures wire delay, arbitration overhead, and other fixed-cost cycles. Each additional word contributes one cycle (one beat on the bus).

### The three access cases

An endpoint's access vocabulary is not a menu of spellings. Every operation falls into one of three
cases, and the case is decided by **what physically happens and therefore what owns the time**. All
three are essential; collapsing any of them models a cost the hardware does not have.

| | Case 1 — timed transfer | Case 2 — pipelined overlap | Case 3 — in place |
|---|---|---|---|
| [`StreamIF`](primitive/stream.md) | `get` / `write` | `get_pipelined` / `write_pipelined` | — no addressing |
| [`MMIF`](primitive/aximm.md) | `read/write_schema`, `read/write_array` | `*_pipelined`, `*_anchored`, `*_spanned` | — every access is a bus transaction |
| [`BramIF`](primitive/bram.md) | *not built* — see below | `read_pipelined` / `write_pipelined` | **`array_ref`** |
| `HwState` | — already local | — | *not built* |

**Case 1 — non-overlapping timed transfer.** Data physically moves into an internal structure. The
endpoint owns a latency model and the call elapses time. This is the model described just above.

**Case 2 — pipelined overlap.** Two transfers that can proceed at once — reading one endpoint while
writing another. The `tstart` anchoring is the whole mechanism: a read hands back the cycle its
*first* word arrived, and `write_pipelined(data, t_start)` treats the write as having begun then,
shortening its wait if `t_start` is already past. So the two phases **overlap** and cost
`max(a, b)` rather than `a + b`, which is what a task that emits a word as it receives one actually
does. There is one anchoring convention and every endpoint uses it.

```python
x, tstart = yield from self.s_in.get_pipelined(Float32, count=n)
y = <numpy over the whole array>              # no element loop anywhere
yield from self.buf_w.write_pipelined(y, addr, tstart)
```

**Case 3 — in place.** Unique to directly-addressable storage, and the reason is **timing, not
copies**. A kernel computing against a BRAM transfers nothing — in C++ it is `foo(&buf[addr], n)`,
reading and writing the memory through its port. Modelling that as a read, a compute and a write
invents two transfers that do not exist and charges the design for them. A stream has no addressing
and every `m_axi` access is a bus transaction, so `BramIF` and `HwState` are the only two citizens.

```python
x = self.buf.array_ref(addr, n)      # a LIVE view -- nothing moved, no simulated time passed
x[:] = x * 3 + 1                     # in place, through one port
yield self.timeout(n * self.buf.ii_for(2) / self.clk.freq)   # 2 accesses/element -> II=2
```

Nothing there elapses time on its own, and that is the point: **the caller owns the timing**,
because the cost is the compute loop's `II x n` rather than a transfer. What the endpoint owes is
the *number* to compute from — `accesses_per_cycle`, and `ii_for()` over it — so the body multiplies
a declared rate instead of a guessed one.

Two things follow, and both are enforced rather than documented:

* **A reference is directional.** `access` already says what the port does, so a `"read"` port's
  view comes back with `flags.writeable = False` and a stray write *raises* instead of silently
  reaching nothing.
* **A reference must never silently become a copy.** `array_ref` is available exactly when the
  element type has a native numpy dtype, and refused otherwise — a composite element is stored as
  its packed word, so referencing it would have to deserialize into a fresh object. The copying
  Case 1 ops are the answer for that element type.

**Vectorized Python, looped HLS, timing carried by the model.** These cases are what make that work:
a design body moves whole vectors and the interface supplies the cycles, while the generated C++
keeps its `#pragma HLS PIPELINE II=1` loop. A per-element `for` in a pysim body is a defect rather
than a fidelity feature — it opts the design out of the model. `examples/stream_inband`'s
`PolyAccel` is the reference, and [`bram_access`](../../examples/bram_access/) is the same shape over
a memory.

Cells marked *not built* are filled as each case ships; see `plans/typed_transfer_codec.md`.
(`BramIF`'s Case 1 has no caller yet, which is why it is deliberately last.)

**All three cases in one design:** [A memory reached three ways](../../examples/bram_access/) is the
worked example. `WRITE` is Case 1 into the memory, `COMPUTE` is Case 3 over it, `READ` is Case 2 out
of it — and because `WRITE` and `COMPUTE` share one port on one task, the difference between moving a
word and computing on it in place is
[a measurement in one waveform](../../examples/bram_access/timing.md#what-it-costs-to-read-a-word-you-are-about-to-write)
rather than an argument.

### The access vocabulary: three verbs, three meanings

The three cases above say *what physically happens*. This says *what the verb is called*, and the
point of the table is that the differences are **deliberate**. A reader meeting `get` on a stream
beside `read` on an `m_axi` port naturally assumes one of them is a leftover; neither is.

| Verb | Means | Where | What it costs the source |
|---|---|---|---|
| `get` | a **destructive dequeue** — the item is gone from the channel | `StreamIFSlave`, `CreditStreamSlaveIF` | the item; nobody else can read it |
| `read` | an **addressed look**, non-destructive — read the same address twice and get the same answer | `MMIFMaster`, `BramIFMaster` | nothing; the storage is unchanged |
| `acquire` | a **lease**, with a matching `release` | `SobIFMaster` (`acquire_write` / `commit_write`), `SobIFSlave` (`acquire_read` / `release_read`) | exclusive use of the block until it is released |

So `get` is not an older spelling of `read`. A queue has no addresses to re-read and a memory has
nothing to consume, and a lease is neither: it hands out a *region* for a while and takes it back.
Rename any one of them to the others and the page stops being able to say which of the three a call
does.

The same distinction is why the pipelined forms are spelled the way they are:
`StreamIFSlave.get_pipelined` beside `BramIFMaster.read_pipelined` and
`MMIFMaster.read_pipelined` — one convergent `_pipelined` suffix, and the verb in front of it still
carries the meaning above.

#### `_nb` is the non-blocking suffix, and `offer` is the deliberate exemption

A transfer that returns *"nothing available"* or *"no room"* instead of blocking carries `_nb`:
`get_nb`, `read_nb`, `write_nb`, `write_resp_nb`, `read_frame_nb`.

`StreamIFMaster.offer` does the same thing and keeps its own name, because the two exist for
**opposite reasons** and the asymmetry is real:

| | who declines to wait | what a refusal means |
|---|---|---|
| `get_nb` | a consumer that **must not** wait — one polling a progress channel, where empty means *"no news"*, not *"stop"* | try again later; nothing was lost |
| `offer` | a producer that **physically cannot** wait — a data converter presents a beat whether or not the fabric is ready | the words that did not fit are **gone**, and `StreamIF.dropped` counts them |

`_nb` says *the caller chose not to wait*, so a short answer is that caller's business to retry.
`offer` says *the producer had no choice*, so there is no retry and the loss is a fact about the run
rather than a return value. Filing both under one suffix would hide that.

Two things that look like exceptions and are not. `can_write_frame` is a **predicate**, not a
transfer — a predicate never blocks, so the suffix would carry no information; what it gates
(`write_frame`) does block, and is correspondingly not `_nb`. And `poll_credit`, `offer_credit`,
`harvest` and `send_status` on the [reverse channels](./derived/) are all non-blocking but named for
*what they do*, because "non-blocking" is already implied by the channel they run on.

### SimPy integration

Interface transactions are modelled as SimPy generator processes. Calling `write` or `read` on a master endpoint returns a generator; the caller must yield it to advance simulation time:

```python
def run_proc(self) -> ProcessGen:
    words = np.array([1, 2, 3], dtype=np.uint32)

    # Blocks until the transfer completes (latency + burst cycles)
    yield self.process(master_ep.write(words))
```

For reads that return data, the result is carried in `proc.value` (the SimPy process return value):

```python
proc = env.process(master_ep.read(nwords=4, global_addr=0x0000))
yield proc
data = proc.value   # numpy array of shape (4,)
```

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s connecting over a `StreamIF` — a `Producer` holding the master
endpoint and a `Consumer` holding the slave endpoint, bound and run in one `Simulation`. No
`HwModule` involved. (The `yield` / `run_proc` / `ProcessGen` mechanics are explained in
[Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class Producer(SimObj):
    """Holds the master endpoint; writes each packet over the stream."""

    master: StreamIFMaster | None = None
    packets: list | None = None

    def run_proc(self) -> ProcessGen[None]:
        for packet in self.packets:
            yield from self.master.write(packet)   # blocks for latency + burst cycles
            print(f"{self.name} sent {packet.tolist()}")


@dataclass
class Consumer(SimObj):
    """Holds the slave endpoint; rx_proc fires for each arriving burst."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[np.ndarray] = []
        self.slave = StreamIFSlave(sim=self.sim, bitwidth=32, rx_proc=self.on_receive)

    def on_receive(self, words: Words) -> ProcessGen[None]:
        self.received.append(np.array(words, copy=True))
        print(f"{self.name} received {words.tolist()}")
        yield self.timeout(0)


sim = Simulation()
iface = StreamIF(sim=sim, clk=Clock(freq=100e6), bitwidth=32)

producer = Producer(
    name="producer", sim=sim,
    master=StreamIFMaster(sim=sim, bitwidth=32),
    packets=[np.array([1, 2, 3], dtype=np.uint32), np.array([4, 5], dtype=np.uint32)],
)
consumer = Consumer(name="consumer", sim=sim)

iface.bind("master", producer.master)
iface.bind("slave", consumer.slave)

sim.run_sim()
print("consumer received:", [p.tolist() for p in consumer.received])
```

Each burst the producer `write`s lands in `consumer.received` (`[[1, 2, 3], [4, 5]]`): the master
drives, the slave's `rx_proc` fires per burst, and `run_sim()` returns once the producer's `run_proc`
finishes and the slave's receive loop parks on its empty buffer. See [SimObj](../sim/simobj.md) for the
base object and lifecycle.

## Available interface types

The full list, in tiers, is the map on the [Interfaces](./) landing page — it is the one summary
table for this section, so this page does not repeat it. In short: a **primitive** interface lowers
to a real HLS construct ([primitive interfaces](./primitive/)), a **derived** one is a transaction
pattern over a primitive ([derived interfaces](./derived/)), and a simulation-only one does not
lower at all.

## Lifecycle

Interfaces participate in the standard SimPy three-phase lifecycle managed by `Simulation`:

1. **`pre_sim()`** — validate bindings, assign address ranges, set up state.
2. **`run_proc()`** — slave endpoints start their receive loops here (e.g. `StreamIFSlave.run_proc()`).
3. **`post_sim()`** — collect statistics or assert invariants.

`assign_address_ranges()` (for `AXIMMCrossBarIF`) should be called after binding but before `sim.run_sim()`.

## Next steps

- [Stream Interfaces](./primitive/stream.md) — unidirectional streaming with `StreamIF` and `CrossBarIF`
- [MM Interfaces](./primitive/aximm.md) — memory-mapped read/write with `AXIMMCrossBarIF` and `DirectMMIF`
