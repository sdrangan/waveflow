---
title: Stream Interfaces
parent: Primitive interfaces
grand_parent: Interfaces
nav_order: 1
audience: python
api: [StreamIF, StreamIFMaster, StreamIFSlave, SimObj, Simulation]
summary: "The point-to-point StreamIF and the four ways to move data over one — a raw word, n raw words, a typed array, or a schema — each with the in-kernel HLS call it corresponds to. Then the cases those four do not cover: a producer that cannot be back-pressured, pipelined get/write timing, and a runnable producer→consumer toy."
---

# Stream Interfaces

Stream interfaces model **unidirectional data bursts** from one component to another. They correspond to AXI4-Stream and Vitis HLS stream bus protocols. This page is about the point-to-point `StreamIF`; the n-input × m-output switching fabric is [CrossBarIF](./crossbar.md).

## Point-to-point: StreamIF

`StreamIF` connects one master endpoint to one slave endpoint. The master calls `write()` to push a burst; the slave either pulls it with `get()` or receives it via an `rx_proc` callback, after the modelled latency.

### Classes

| Class | Role | Key parameters |
|---|---|---|
| `StreamIF` | Interface | `clk`, `bitwidth`, `latency_init` |
| `StreamIFMaster` | Master endpoint | `bitwidth` |
| `StreamIFSlave` | Slave endpoint | `bitwidth`, `rx_proc`, `queue_size` |

### The four ways to move data

Almost everything you do with a stream is one of these four, and **both directions are one method** —
`write()` on the master, `get()` on the slave. What changes is the argument.

| what you are moving | master | slave |
|---|---|---|
| **one raw word** | `write(np.array([x], dtype=np.uint64))` | `words = yield from get(nwords_max=1)` |
| **n raw words** | `write(words)` | `words = yield from get(nwords_max=n)` — or `get()` for a whole burst |
| **an array** of a schema element | `write(array(Float32, xs))` | `arr = yield from get(Float32, count=n)` |
| **a schema** (a command, header, response) | `write(cmd)` | `cmd = yield from get(FirCmd)` |

Both block: the master waits for room, the slave waits for data. That is the AXI-Stream contract, and
it is what lets a graph of tasks compose without anyone counting.

**The last two rows are the same call.** `write()` serializes *any* `DataSchema` instance at the
interface's `bitwidth`, and a `DataArray` is one — so you hand it the object, never a list of words.
On the read side, `get(Schema)` derives the word count from `Schema.nwords_per_inst(bitwidth)`, and
`get(Schema, count=n)` returns a `DataArray`.

```python
yield from self.s_out.write(cmd)          # a schema instance, serialized for you
cmd = yield from self.s_in.get(FirCmd)    # one call, deserialized for you
```

> **Do not take a structured message apart a word at a time.** A command, header or response is a
> [`DataList`](../../vectorization/); declare it once and let `get(Schema)` read it. Pulling `n` words
> with `n` calls re-authors the field layout in a second place — and if the schema carries an
> `include_filename`, that second place silently disagrees with the **generated C++ header the kernel
> compiles against**. `examples/stream_inband/poly.py` is the worked form.

#### The same four in HLS

Every row has a kernel-side twin, so a hook and its Python model can be read against each other. The
in-kernel calls are collected in the
[kernel transfer reference](../../custom_hooks/reference.md#mapping-the-python-transfer-interfaces-to-the-kernel):

| tier | Python | HLS |
|---|---|---|
| one raw word | `get(nwords_max=1)` / `write([x])` | `s.read()` / `s.write(x)` |
| n raw words | `get(nwords_max=n)` | a counted loop over `s.read()` |
| an array | `get(Elem, count=n)` | `au::read_stream_lane<W>(s, out, n)` / `write_stream_lane<W>(src, s, n)` |
| a schema | `get(Schema)` / `write(obj)` | `Schema::read_stream<W>(s)` / `obj.write_stream<W>(s)` — from the header the schema generates |

`au` is the generated `<element>_array_utils` namespace. On a **framed** or **AXI4-Stream** port the
array row becomes `read_framed_stream_lane` / `read_axi4_stream_lane` (and their writes), which carry
`TLAST`; see [array utils](../../vectorization/hls/arrayutils.md#the-three-framings--all-three-now-covered).

The rest of this section is what these four do not cover: a producer that cannot wait, and a consumer
whose processing time you want modelled.

### Latency model

```
transfer_time = (latency_init + nwords) / clk.freq   [seconds]
```

- `latency_init` — fixed cycles for wire delay, arbitration, etc.
- `nwords` — one additional cycle per word in the burst (one beat per clock)

### A producer that cannot wait: `offer()` {#offer}

`write()` **waits** when the consumer is full, which is what almost every producer in a design does —
back-pressure is the AXI-Stream contract. A few producers physically cannot: an ADC hands over the
samples it has converted, and whatever the fabric is not ready for is *gone*.

```python
accepted = yield from self.rx_stream.offer(block, word_rate=samp_rate / samp_per_word)
```

- **Non-blocking.** Returns the number of words the consumer accepted; the rest are dropped.
- **The interface counts them.** `StreamIF.dropped` accumulates dropped words and `last_drop_time`
  records when it last happened — on the *edge*, so a checker reads one number regardless of which
  producer is on the other end.
- **`word_rate`** paces the transfer at the producer's own rate rather than the interface clock. A
  converter's block takes `nwords / (samp_rate / samp_per_word)` seconds to exist; charging it at the
  fabric clock claims it crossed in a fraction of that and hands the consumer drain time the hardware
  never gives it.

There is deliberately **no separate interface type** for this. A plain AXI-Stream is what is on the
wire either way — "who is willing to wait" is a property of the producer, not of the link.

`dropped == 0` is therefore the mechanical form of *"this consumer never stalls its input"*, and it
stays zero for every design whose producers call `write()`. It has a resolution limit worth knowing
before you rely on it: see [the fidelity boundary](../../rf/rfdc/fidelity.md#the-resolution-limit).

### Pipelined processing

Use `get_pipelined` / `write_pipelined` when the component processes data as it streams through (rather than buffering the full burst first). These methods carry pipeline timing explicitly so the simulation reflects the latency and throughput of synthesized hardware.

**`StreamIFSlave.get_pipelined(schema_type, count=N)`** returns `(data, tstart)`:

- `data` — deserialized burst, identical to `get(schema_type, count=N)`
- `tstart` — SimPy time when the **first** word of the burst arrived

`tstart` is back-calculated from the completion time of the burst:

```
tstart = env.now - (nwords_transferred - 1) * clk.period
```

This is exact for a back-pressure-free, II=1 input stream.

**`StreamIFMaster.write_pipelined(data, t_out_start, ii=1)`** waits until `t_out_start` before beginning the write:

```
t_out_start = tstart + proc_latency * clk.period
```

`ii` documents the output initiation interval (informational; reserved for future per-word output pacing).

**Skeleton for a pipelined `evaluate`:**

```python
@synthesizable
def evaluate(self, cmd_hdr, s_in, m_out):
    resp_hdr = PolyRespHdr()
    resp_hdr.tx_id = cmd_hdr.tx_id
    yield from m_out.write(resp_hdr)

    samp_in, tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)

    # ... compute output y from samp_in ...

    t_out_start = tstart + self.proc_latency * self.clk.period
    yield from m_out.write_pipelined(
        array(Float32, y), t_out_start, ii=self.proc_ii
    )
    if len(samp_in) != cmd_hdr.nsamp:
        return PolyError.WRONG_NSAMP
    return PolyError.NO_ERROR
```

Set `proc_ii` and `proc_latency` on the component to match values reported by HLS synthesis for the `evaluate` loop.

### Example: point-to-point stream

```python
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class Producer(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFMaster(sim=self.sim, bitwidth=32)

    def run_proc(self) -> ProcessGen:
        for i in range(3):
            words = np.array([i * 10, i * 10 + 1], dtype=np.uint32)
            yield self.process(self.ep.write(words))


@dataclass
class Consumer(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[np.ndarray] = []
        self.ep = StreamIFSlave(
            sim=self.sim,
            bitwidth=32,
            rx_proc=self.on_rx,
            queue_size=16,
        )

    def on_rx(self, words: Words) -> ProcessGen:
        self.received.append(words.copy())
        yield self.env.timeout(0)   # or model processing delay here

    def run_proc(self) -> ProcessGen:
        yield from self.ep.run_proc()


sim = Simulation()
clk = Clock(freq=100e6)

producer = Producer(sim=sim)
consumer = Consumer(sim=sim)

iface = StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)
iface.bind("master", producer.ep)
iface.bind("slave",  consumer.ep)

sim.run_sim()
```

The `Consumer.run_proc()` must delegate to `ep.run_proc()` so the slave's receive loop is active during simulation. `Simulation.run_sim()` calls each `SimObj.run_proc()` automatically, so this pattern wires together correctly.

This is a complete, runnable two-[`SimObj`](../../sim/simobj.md) simulation — a `Producer` (master) and a `Consumer` (slave) bound over one `StreamIF`, no `HwModule` — and it is the same shape as the toys on the other interface pages. The `yield` / `run_proc` / `ProcessGen` mechanics it relies on are explained in [Process generators](../../sim/procgen.md). A [`CrossBarIF`](./crossbar.md) variant is the same idea with port-indexed endpoints (`in_0` / `out_0`).

---

## Crossbar: several producers, several consumers

A `StreamIF` is point-to-point. For an n-input x m-output switching fabric, see
[Crossbar Interfaces](./crossbar.md) — the routing function and a runnable 2x2 example. What you can
move over it is the same four things as above.

## Common patterns

### Checking data in a slave

```python
class Checker(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.bursts: list[np.ndarray] = []
        self.ep = StreamIFSlave(sim=self.sim, bitwidth=32, rx_proc=self.rx_proc)

    def rx_proc(self, words: Words) -> ProcessGen:
        self.bursts.append(words.copy())
        yield self.env.timeout(0)

    def run_proc(self) -> ProcessGen:
        yield from self.ep.run_proc()

    def post_sim(self) -> None:
        assert len(self.bursts) == expected_count
```

### Modelling receiver processing delay

Set a non-zero delay in `rx_proc` to model the time the slave spends consuming each burst:

```python
def rx_proc(self, words: Words) -> ProcessGen:
    processing_cycles = len(words) * 2
    yield self.timeout(processing_cycles / self.clk.freq)
```

### Queue depth

`queue_size` on the slave endpoint bounds how many words can be in-flight. Setting `queue_size=None` (default) gives an unbounded queue. For backpressure modelling, set an explicit depth.

---

## Quick reference

```python
from waveflow.hw.interface import (
    StreamIF, StreamIFMaster, StreamIFSlave,
    StreamGetPipelinedStmt, StreamWritePipelinedStmt,
    Words,
)
from waveflow.hw.clock import Clock
```

| Operation | Code |
|---|---|
| Create interface | `StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)` |
| Create master ep | `StreamIFMaster(sim=sim, bitwidth=32)` |
| Create slave ep | `StreamIFSlave(sim=sim, bitwidth=32, rx_proc=fn)` |
| Bind | `iface.bind("master", ep)` |
| Write (from run_proc) | `yield self.process(ep.write(words))` |
| Start slave loop | `yield from ep.run_proc()` |

---

## How it lowers

A `StreamIFSlave` is an `axis_in` boundary port and a `StreamIFMaster` an `axis_out` one; on an
*internal* edge the same pair is an `hls::stream` FIFO instead, derived from the interface type by a
separate walk. This page is the Python model — see
[the guide's three arcs](../../index.md#how-this-guide-is-organized) for why that split exists.

- **HLS** — [Endpoint interfaces](../../comp_codegen/interface.md#stream-endpoints--axis) for the
  port and its pragma; [Free-running composites](../../comp_codegen/freerunning_composite.md#1-add_if-became-the-channels)
  for the internal-FIFO case.
- **Writing the body** — [Stream — process as you read](../../custom_hooks/stream.md), the lane loop
  over the port.
- **BFM / XSI** — [The XSI testbench](../../comp_codegen/xsi_tb.md#two-walks): `axis_in` gets an
  `AxisMaster`, `axis_out` an `AxisSlave`, from `BFM_DUALS`.
