---
title: Stream drivers and sinks
parent: Simulation
nav_order: 4
audience: python
api: [StreamDriver, StreamSink]
summary: "The two reusable testbench participants: StreamDriver plays a burst bundle (an on-disk vector folder) onto a stream, StreamSink collects words off one. Both are schema-blind SimObjs -- the testbench serializes its own commands and writes the bundle -- and both are the pysim twins of the XSI AxisMaster / AxisSlave, so one graph, and one file of vectors, runs either as fast SimPy events or as cycle-accurate RTL."
---

# Stream drivers and sinks

A testbench needs two things around the design it exercises: something to **drive stimulus** into an
input stream, and something to **collect** what comes out of an output stream. Waveflow ships those
two ends as reusable `SimObj`s in
[`waveflow/simulation/stream_tb.py`](../../../waveflow/simulation/stream_tb.py):

- **`StreamDriver`** — a *source*. It plays the words of a **burst bundle** onto a stream, one burst
  after another, and lets the consumer take them as fast as it accepts.
- **`StreamSink`** — a *sink*. It is always ready and keeps every word it is handed.

They are framework code, not example code: nothing in them knows about any particular kernel, so the
same two classes drive and collect for every design. They are also the **Python twins of the XSI BFM
models** — a `StreamDriver` is at RTL what an `AxisMaster` is; a `StreamSink`, an `AxisSlave` (see
[the RTL twin](#the-rtl-twin) below).

## Vectors are a file, not a Python list

A `StreamDriver`'s only vector input is a **burst bundle** — a folder written by
[`waveflow/utils/burst_io.py`](../../../waveflow/utils/burst_io.py) holding `words.bin`, `bounds.bin`,
and a small `meta.json`. It does not accept an in-memory array, and it never sees a
[schema](../schema/).

That restriction is the point. If the driver took a Python list, the RTL testbench could not use it —
the words would have to be translated into C++ somehow (the old approach baked them into a generated
header). A **file** needs no translation: the pysim `StreamDriver` reads the bundle, and its RTL twin
(`AxisMaster`) reads the *same* bundle. So pysim and RTL provably drive from the **same bytes**, not
merely from the same generator.

The testbench is the one place that knows the schema, so it does the conversion — serialize, write the
bundle, point the driver at it:

```python
cmds = [CopyCmd(src_off=s, dst_off=d, n_words=n) for s, d, n in jobs]   # the TB owns the schema
words = [np.asarray(c.serialize(word_bw=64), dtype=np.uint64) for c in cmds]   # -> raw word bursts
write_burst_bundle(words, vectors_dir / "cmd")                          # -> a bundle on disk
driver = StreamDriver(sim=sim, bitwidth=64, bundle=vectors_dir / "cmd") # the driver just plays it
```

`serialize(word_bw=64)` packs each `CopyCmd` exactly the way the stream endpoint would, so the words
in the bundle are what the DUT expects. A `StreamSink` likewise collects raw words; turning them back
into a schema (if you want to) is again the testbench's job.

The bundle is read **eagerly**, when the driver is constructed — so its files only need to exist at
that moment. A testbench that generates vectors on the fly can write them to a temporary directory and
let it go away, as the example below does.

## A minimal simulation

Both classes are ordinary `SimObj`s, so the smallest useful example is a driver wired straight to a
sink through one stream FIFO — no DUT at all. The testbench writes a two-burst bundle; the driver
plays it; the sink collects it:

```python
import tempfile
from pathlib import Path

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

sim = Simulation()
clk = Clock(freq=100e6)

# Two bursts of raw words -- the schema-blind driver only ever sees a bundle.
bursts = [np.array([1, 2, 3], dtype=np.uint64),
          np.array([10, 20], dtype=np.uint64)]

with tempfile.TemporaryDirectory() as d:
    write_burst_bundle(bursts, Path(d) / "cmd")               # vectors -> a bundle on disk
    driver = StreamDriver(name="drv", sim=sim, bitwidth=64, bundle=Path(d) / "cmd")
    # the bundle is read here, eagerly, so the temp dir can go away now

sink = StreamSink(name="snk", sim=sim, bitwidth=64)

# One FIFO wiring the driver's master endpoint to the sink's slave endpoint.
link = StreamIF(name="link", sim=sim, clk=clk, bitwidth=64)
link.bind(ep_name="master", endpoint=driver.stream_ep)
link.bind(ep_name="slave", endpoint=sink.stream_ep)

sim.run_sim()
print("received", len(sink.words), "bursts:", [w.tolist() for w in sink.words])
```

```text
received 2 bursts: [[1, 2, 3], [10, 20]]
```

Each participant owns a single endpoint, `stream_ep` — a `StreamIFMaster` on the driver, a
`StreamIFSlave` on the sink — and a [`StreamIF`](../interface/) binds one master to one slave, exactly
as it would between two components. `sink.words` is the list of word arrays the sink received, one per
burst.

## Driving a DUT

In a real testbench the driver and sink sit on either side of the design under test rather than back to
back. The pattern is the same `StreamIF` binding — the driver's `stream_ep` master feeds the DUT's
input slave; the DUT's output master feeds the sink's `stream_ep` slave:

```python
cmd_if = StreamIF(name="cmd_if", sim=sim, clk=clk, bitwidth=64)
cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)   # driver -> DUT input
cmd_if.bind(ep_name="slave",  endpoint=dut.s_cmd)

done_if = StreamIF(name="done_if", sim=sim, clk=clk, bitwidth=64)
done_if.bind(ep_name="master", endpoint=dut.s_done)        # DUT output -> sink
done_if.bind(ep_name="slave",  endpoint=sink.stream_ep)
```

Because the driver never waits for a *response*, downstream jobs are free to overlap — the source keeps
offering the next command the moment the DUT accepts the last, so a pipelined kernel is already working
on job *j+1* while it finishes job *j*. That non-blocking property is the whole reason the concurrent
flow's testbench looks the way it does; a full worked example is
[`examples/mem_copy/mem_copy_sim.py`](../../../examples/mem_copy/mem_copy_sim.py) (the
[Memory Copy](../../examples/memcpy/) example).

## The RTL twin

The same testbench graph runs two ways. In pysim, a `StreamDriver`/`StreamSink` moves words on SimPy
events. At the RTL rung, each participant declares a **BFM twin** via `bfm_model()` — a `StreamDriver`
becomes an [`AxisMaster`](../build/bfm.md), a `StreamSink` an `AxisSlave` — that drives the elaborated
Verilog cycle by cycle through XSI. Same words, same wiring; only the timing model differs (fast
transaction events vs. exact per-cycle handshakes).

This is why one design description serves both: the [concurrent flow](../flows/concurrent.md) runs the
identical graph as a fast Python check and as a cycle-accurate RTL test — and, because the driver plays
a bundle, from the identical vectors too. The `AxisMaster` reads that same bundle directly: the
participant carries an `in_bundle` [`DynParam`](../../../waveflow/hw/hw_component.py) which the
generated harness emits as `s_cmd.in_bundle = "…";`, and the model loads it in `pre_sim`. No vectors are
baked into a generated header, and neither side owns a second copy of the stimulus.

How those models are composed into a runnable testbench — generated from the graph, or assembled by
hand — is [BFM Testbenches](../build/bfm.md).

## Quick reference

- `StreamDriver(sim=, bitwidth=, bundle=<dir>)` — a source; `bundle` is a burst-bundle folder
  ([`burst_io`](../../../waveflow/utils/burst_io.py)), read eagerly at construction. Its endpoint is
  `stream_ep` (a master). Set `in_bundle` to give its RTL twin the same bundle at the XSI rung.
- `StreamSink(sim=, bitwidth=)` — a sink; collects into `sink.words` (a list of word arrays). Its
  endpoint is `stream_ep` (a slave). Set `out_bundle` and its RTL twin dumps what it captured — words
  plus the arrival cycle of each — for Python to check after the run.
- Both are schema-blind: the testbench serializes its commands
  (`[c.serialize(word_bw=bitwidth) for c in cmds]`) and writes the bundle with
  `write_burst_bundle(...)` before constructing the driver.
- Bind each `stream_ep` with a [`StreamIF`](../interface/), master to slave.
- `bfm_model()` gives the RTL twin (`AxisMaster` / `AxisSlave`) for the [XSI testbench](../build/bfm.md).
