---
title: Python
parent: Interleaver (gather)
nav_order: 2
---
# Python Model

This page builds the design in Python — the four hand-written [`FreeRunMod`](../../guide/flows/concurrent.md)
leaves, the two framework mem-streams they compose with, and the composite that wires all six — and shows
*how to write each kind of block* for a gather `Y[i] = X[P[i]]`. The whole design is
`examples/interleaver/interleaver_inband.py`.

## The command and descriptor structures

Every stream carries a typed [schema](../../guide/schema/). Two descriptor types split the work, following
`mem_copy`'s convention — one **plain** boundary command the host sends, one **framed** internal descriptor
the pipeline forwards. The **application command** on `s_cmd` is an `InterleaverCmd` (`interleaver.py`):
three buffer offsets and a length, all element/word coordinates (not byte addresses):

```python
class InterleaverCmd(DataList):
    elements = {
        "p_off": {"schema": Word32, "description": "P (index) buffer word offset"},
        "x_off": {"schema": Word32, "description": "X (source) buffer word offset"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
        "n":     {"schema": Word32, "description": "number of elements to interleave"},
    }
```

The **framed internal descriptor** forwarded through the pipeline is `IlDesc` (`interleaver_inband.py`) —
only the two facts the downstream stages need: the length `n` (so the RTL is variable-length,
scenario-independent) and the output offset `y_off` the writer targets:

```python
class IlDesc(DataList):                       # framed — read/written with the framed method set
    include_filename: ClassVar[str | None] = "il_desc.h"
    elements = {
        "n":     {"schema": Word32, "description": "number of elements this job gathers"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
    }
```

The two mem-stream stages act on framework descriptors `MemRCmd` / `MemWCmd` (`waveflow/hw/mem_stream.py`),
the same **shape** — an address, a length, and `fwd_bursts` (how many following bursts this stage relays):

```python
class MemRCmd(DataList):                 # what MemRStream acts on
    elements = {
        "addr":       {"schema": Word32, ...},   # element offset to read from
        "len":        {"schema": Word32, ...},   # words to fetch
        "fwd_bursts": {"schema": Word32, ...},   # opaque bursts to relay BEFORE the data
    }

class MemWCmd(DataList):                 # what MemWStream acts on (same shape — the chain is symmetric)
    elements = {
        "addr":       {"schema": Word32, ...},   # element offset to write at
        "len":        {"schema": Word32, ...},   # words to write
        "fwd_bursts": {"schema": Word32, ...},   # opaque bursts to buffer across the write, then echo
    }
```

**The descriptor split is the point.** `InterleaverCmd` is the plain boundary command — it is *never*
framed. Every *internal* stream is framed, and the only schema that rides an internal stream is `IlDesc`
(plus the framework `MemRCmd` / `MemWCmd`). So no schema needs both a boundary and a framed method set: the
boundary is plain, everything past it is framed, and the two never mix on one type.

## The forwarding chain: two reads through one arbiter

Like `mem_copy`, the interleaver welds each command to its data on **one framed stream in forwarding order**,
and each stage strips the descriptor addressed to it and relays the rest opaquely. The interleaver's twist is
that its input is *two* buffers — the indices `P` and the source `X` — so `cmd_rx` frames the reader's command
stream as **two reads**:

```python
memr_p = MemRCmd(addr=int(cmd.p_off), len=nw, fwd_bursts=1)   # read P, relay 1 burst (the descriptor)
memr_x = MemRCmd(addr=int(cmd.x_off), len=nw, fwd_bursts=0)   # read X, relay nothing
yield from self.cmd_out.write(...memr_p...)                   # [ MemRCmd(P,fwd=1)
yield from self.cmd_out.write(...desc...)                     #   | IlDesc
yield from self.cmd_out.write(...memr_x...)                   #   | MemRCmd(X,fwd=0) ]
```

`MemRStream` moves *one region per firing*, so it fires **twice** per job: the first read (`fwd_bursts=1`)
relays the `IlDesc` header, then bursts `P`; the second read (`fwd_bursts=0`) relays nothing and bursts `X`.
Its output is the single framed stream `[IlDesc | P | X]`. This is the **transactional-arbiter** model — a
consumer issues *N* reads per job through the one read owner (contrast `mem_copy`, which needs exactly one
read and one write). `P` goes first so `il_load` fills `p_blk` before `x_blk`, matching the read-lock order in
`il_compute`.

The `fwd_bursts=0` second read relies on the reader's `if (nfwd > 0)` relay guard: without it a bare relay
loop emits one *phantom* word and the RTL deadlocks — a bug the pysim for-loop never had, which is why it
passed while the hardware wedged. (Full story on the [Module Overview](./interleaver.md#message-forwarding-the-in-band-descriptor).)

## Why a stream of blocks

This is the structural difference from `mem_copy`. A stream is consumed in order, but the gather's defining
move is `X[P[i]]` — an **arbitrary** index — so `X` cannot stay a stream. `il_load` lands both `P` and `X`
into on-chip block RAMs exposed as a [stream of blocks](../../guide/concurrency/python/sob.md) (a ping-pong
pair with a lock handshake, so `il_load` can fill the next job's block while `il_compute` still reads this
one). The blocks are **typed element blocks** — `DataArray` of 32-bit `Word32`, i.e. `ap_uint<32>[N]` in
C++ — so `il_compute` is the bare gather with no lane math, and the word↔element (de)serialization lives in
`il_load` / `il_store` instead:

```python
def _make_elem_block(n: int) -> type:
    return DataArray.specialize(element_type=Word32, max_shape=(int(n),), member_name="elems")
```

## Writing a leaf: `il_compute`

A leaf is a `FreeRunMod`. You declare its endpoints in `__post_init__` and implement `run_iter` (one firing;
the runtime re-fires it per job, so there is no command loop). `il_compute` reads `IlDesc`, holds read locks
on `p_blk` / `x_blk` and a write lock on `y_blk`, and does the gather in one vectorized fancy-index:

```python
class IlComputeInband(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "il_compute"

    def run_iter(self) -> ProcessGen[None]:
        desc = yield from self.desc_in.get(IlDesc)
        n = int(desc.n)
        yield from self.desc_out.write(...desc...)          # forward the descriptor
        pblock = yield from self.p_blk.acquire_read()
        xblock = yield from self.x_blk.acquire_read()
        yblock = yield from self.y_blk.acquire_write()
        yblock.val[:n] = xblock.val[pblock.val[:n]]         # the gather, vectorized: Y[i] = X[P[i]]
        cycles = float(self.timing.predict({"n": n}))       # its own fitted loop-timing model
        yield self.timeout(max(0.0, cycles) * self.clk.period)
        ...
```

The four stages here — `cmd_rx`, `il_load`, `il_compute`, `il_store` — are all **hand-written** leaves.
Each `run_iter` is the *pysim golden* (the behaviour the simulation runs), and each has a fixed, hand-written
C++ twin copied in by the build — `il_cmd_rx_framed_task.h`, `il_load_inband_task.h`,
`il_compute_inband_task.h`, `il_store_inband_task.h` in `waveflow/build/` — **not** generated from `run_iter`.
Three things put them on the hand-written side of the line: they construct `DataSchema`s (`MemRCmd(...)`,
`IlDesc(...)`), they drive *framed* channels, and they use *stream-of-blocks*. None is in the code
generator's vocabulary, so keeping each Python golden and its C++ in agreement is your job; only the tests
check it. (`il_compute_inband_task.h`'s body is the one line `yb[i] = xb[pb[i]]` under `#pragma HLS PIPELINE
II=1` — the pysim gather laid bare.)

## The mem-stream leaves

`MemRStream` and `MemWStream` are also `FreeRunMod` leaves, but you do **not** write them — they are
framework components. Each owns an `m_axi` port (`m_mem`) and a data stream, and its body is a hand-written,
width-templated `hls::task` copied in by the build (a body that owns a memory port is never lowered from
`run_iter` — the dividing line is `m_axi`). Both are constructed `inband=True`: the reader takes its
`MemRCmd` in-band and relays the opaque prefix; the writer takes its `MemWCmd` in-band on its single data
stream. The writer is additionally `emit_done=True`, so it buffers the echoed `IlDesc` across the store and
emits it on `s_done` after the write commits — the commit-timed completion. Their full story is
[Streaming Memory Kernels](../../guide/memory/memstream.md).

## Writing the composite: `InterleaverInband`

The composite is a `FreeRunMod` with sub-components instead of a `run_iter` body — that is what "composite"
means here. It has **no `run_iter`**; its children do the work, and it only declares structure. Three things,
all in `__post_init__`.

**1. Add the six sub-components** in dataflow order (insertion order is the generated `hls::task` order):

```python
for c in (self.rx, self.rstream, self.load, self.compute, self.store, self.wstream):
    self.add_comp(c)      # cmd_rx → MemRStream → il_load → il_compute → il_store → MemWStream
```

**2. Wire the internal edges** with `add_if`. There are three kinds — **five framed stream edges** (each an
on-chip `framed_word` FIFO), and **three stream-of-blocks edges** (each a ping-pong block RAM):

```python
_sif("cmd_rd", self.rx.cmd_out,      self.rstream.s_cmd)     # [MemRCmd | IlDesc | MemRCmd]  framed
_sif("rdata",  self.rstream.m_out,   self.load.s_in)         # [IlDesc | P | X]              framed
_sif("desc_lc", self.load.desc_out,    self.compute.desc_in) # IlDesc                        framed
_sif("desc_cs", self.compute.desc_out, self.store.desc_in)   # IlDesc                        framed
_sif("wdata",  self.store.cmd_out,   self.wstream.s_in)      # [MemWCmd | IlDesc | Y]        framed
_sobif("p_blk", self.load.p_blk,    self.compute.p_blk)      # stream-of-blocks
_sobif("x_blk", self.load.x_blk,    self.compute.x_blk)      # stream-of-blocks
_sobif("y_blk", self.compute.y_blk, self.store.y_blk)        # stream-of-blocks
```

(`_sif` builds a `StreamIF(framed=True)`; `_sobif` builds a `StreamOfBlocksIF` over the typed element block.
The three mem-stream edges are framer→reader, reader→load, and store→writer; the two middle `IlDesc` edges
carry the descriptor through the compute.)

**3. Name the boundary ports** — the four endpoints that leave the top:

```python
self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
self.s_cmd  = self.rx.s_cmd
self.m_in   = self.rstream.m_mem      # → gmem0
self.m_out  = self.wstream.m_mem      # → gmem1
self.s_done = self.wstream.s_done
```

Everything else about the graph is *derived* from `add_comp` / `add_if`, exactly as in `mem_copy`: which
children become `hls::task`s and in what order (`add_comp` order), the internal FIFOs and their C++ names
(each `add_if` interface *is* an edge, named after itself), how each edge lowers (the interface's type/flags
— `framed=True` → a `framed_word` FIFO, `StreamOfBlocksIF` → a ping-pong block RAM), and each port's
direction (the endpoint's type). Only the boundary *names* are yours to say, because both mem-streams call
their AXI port `m_mem`, so the top's `m_in` / `m_out` have to be stated and get the `gmem0` / `gmem1` bundles
by policy.

That is the whole composite: six children, eight internal edges, four port names, and no body of its own.
Running it in Python — the `InterleaverInband` graph plus a testbench — is the concurrent simulation, and
walking that same graph is how the generated kernel is built.

## Next

[Testbench (Python)](./testbench.md) — the graph that surrounds this design, and running it in pysim. Then
[DUT codegen](./codegen_dut.md) — how this graph becomes the `ap_ctrl_none` top, and why every task body
here is hand-written.
