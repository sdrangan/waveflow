---
title: Python
parent: Memory Copy
nav_order: 2
---
# Python Model

This page builds the design in Python — the three [`FreeRunMod`](../../guide/flows/concurrent.md)
leaves and the composite that wires them — and shows *how to write each kind of block*. The whole design
is `examples/mem_copy/mem_copy.py`.

## The command and descriptor structures

Every stream in the design carries a typed [schema](../../guide/schema/). The **application command**
the host sends on `s_cmd` is a `CopyCmd` — a source offset, a destination offset, a length, and a
transaction id, all element/word coordinates (not byte addresses):

```python
class CopyCmd(DataList):
    elements = {
        "src_off": {"schema": Word32, "description": "source element/word offset"},
        "dst_off": {"schema": Word32, "description": "destination element/word offset"},
        "n_words": {"schema": Word32, "description": "number of packed words to copy"},
        "tx_id":   {"schema": Word32, "description": "host transaction ID, echoed on completion"},
    }
```

(Four `Word32`s pack into two 64-bit words at `MEM_DW=64`, so `tx_id` costs no extra command words — it
fills what was padding.)

The `Sequencer` turns each `CopyCmd` into a **framed command stream** — one descriptor per stage,
plus a typed response, all on one stream: `[MemRCmd | MemWCmd | CopyResp]`. The two mem-stream
descriptors are framework schemas with the **same shape** — an address, a length, and `fwd_bursts`
(how many following bursts this stage relays):

```python
class MemRCmd(DataList):                # what MemRStream acts on
    elements = {
        "addr":       {"schema": Word32, ...},   # element offset to read from
        "len":        {"schema": Word32, ...},   # words to fetch
        "fwd_bursts": {"schema": Word32, ...},   # opaque bursts to relay before appending the data
    }

class MemWCmd(DataList):                # what MemWStream acts on (same shape — the chain is symmetric)
    elements = {
        "addr":       {"schema": Word32, ...},   # element offset to write at
        "len":        {"schema": Word32, ...},   # words to write
        "fwd_bursts": {"schema": Word32, ...},   # opaque bursts to buffer across the write, then echo
    }
```

The **response** the writer emits on `s_done` is a typed `CopyResp`, mirroring the typed `CopyCmd`
request — one field for now, the `tx_id` echoed back:

```python
class CopyResp(DataList):
    elements = {
        "tx_id": {"schema": Word32, ...},        # the request's transaction ID, echoed on completion
    }
```

## The forwarding chain: one stream, each command welded to its data

The stages are free-running, so several jobs are in flight at once — and each stage's command must never
pair with the wrong data. Two independent ports (a command stream and a data stream) is the trap: no
shared boundary, so a dropped or extra command silently shifts every later job's data onto the wrong
descriptor. `MemCopy` avoids it by putting everything **on one stream, in forwarding order** —
`[MemRCmd | MemWCmd | CopyResp]` — and having each stage **strip the one descriptor addressed to it and
relay the rest opaquely**:

- the reader reads `MemRCmd`, relays its `fwd_bursts` bursts (the `MemWCmd` + the `CopyResp`), then
  appends the `len` words it fetched;
- the writer reads `MemWCmd`, buffers its `fwd_bursts` bursts (the `CopyResp`) across the write, stores
  the data, then emits the `CopyResp` on `s_done`.

A command physically cannot separate from the data it describes, and a relaying stage never parses what
it forwards — so the mem-streams stay application-agnostic and the `Sequencer` is the only schema-aware
stage (the full picture, with the peeling diagram, is on the [Module Overview](./memcpy.md)).
Correlation rides the same stream: `CopyResp` carries the host's `tx_id`, echoed on completion; because
the tag comes from the command, the `Sequencer` holds **no cross-firing state**.

## Writing a leaf: the `Sequencer`

A leaf is a `FreeRunMod`. You declare its endpoints in `__post_init__` and implement `run_iter` (one
firing; the runtime re-fires it per job, so there is no command loop). The `Sequencer` has one input and
one **framed** output:

```python
class Sequencer(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "mem_seq"
    mem_dwidth: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd   = StreamIFSlave (name=f"{self.name}_s_cmd",   sim=self.sim,
                                      bitwidth=self.mem_dwidth, has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim,
                                      bitwidth=self.mem_dwidth, has_tlast=True)   # framed
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get(CopyCmd)                       # one command
        memr = MemRCmd(addr=int(cmd.src_off), len=int(cmd.n_words), fwd_bursts=2)
        memw = MemWCmd(addr=int(cmd.dst_off), len=int(cmd.n_words), fwd_bursts=1)
        resp = CopyResp(tx_id=int(cmd.tx_id))
        yield from self.cmd_out.write(np.asarray(memr.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(resp.serialize(word_bw=w), dtype=np.uint64))
```

The `fwd_bursts` values are the whole protocol in two numbers: `MemRCmd.fwd_bursts=2` (the reader relays
the `MemWCmd` + the `CopyResp`), `MemWCmd.fwd_bursts=1` (the writer relays the `CopyResp`).

**This is a hand-written leaf.** `run_iter` is the *pysim golden* — the behaviour the simulation runs —
and its C++ twin is a fixed, hand-written task body (`mem_seq_framed_task.h`, copied in by the build),
**not** generated from `run_iter`. Two things put it on the hand-written side of the line: it constructs
`DataSchema`s (`MemRCmd(...)`, …) and it drives a *framed* channel — neither is in the code generator's
vocabulary. Keeping the Python golden and its C++ in agreement is your job; only the tests check it.

> **The other kind of leaf.** A *stream-only* leaf whose `run_iter` is just `get` → `@synthesizable`
> hook → `write` can instead have its body **generated** from `run_iter` (`TaskBodyStep`). `MemCopy`
> has none — all three leaves here are hand-written — but that generated-leaf pattern is described in
> [DUT codegen](./codegen_dut.md).

## The mem-stream leaves

`MemRStream` and `MemWStream` are also `FreeRunMod` leaves, but you do **not** write them — they are
framework components. Each owns an `m_axi` port (`m_mem`) and a data stream, and its body is a
hand-written, width-templated `hls::task` copied in by the build (a body that owns a memory port is
never lowered from `run_iter` — the dividing line is `m_axi`). Here both are constructed `inband=True`:
the reader reads a `MemRCmd` and relays the opaque prefix, and the writer takes its `MemWCmd` in-band on
its single data stream and forwards the buffered response. Neither parses what it relays. Their full
story is [Streaming Memory Kernels](../../guide/memory/memstream.md).

## Writing the composite: `MemCopy`

The composite is a `FreeRunMod` with sub-components instead of a `run_iter` body — that is what
"composite" means here. It has **no `run_iter`**; its children do the work, and it only declares
structure. Three things, all in `__post_init__`.

**1. Add the sub-components** (insertion order is the generated `hls::task` order):

```python
class MemCopy(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "mem_copy"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.seq     = Sequencer (name=f"{self.name}_seq", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_r",   sim=self.sim, mem_dwidth=w,
                                  inband=True, clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_w",   sim=self.sim, mem_dwidth=w,
                                  emit_done=True, inband=True, clk=self.clk)
        for c in (self.seq, self.rstream, self.wstream):
            self.add_comp(c)
```

**2. Wire the internal streams** with `add_if` — **two framed edges**, each binding a master endpoint on
one child to a slave on another, each becoming an on-chip `framed_word` FIFO in the generated top:

```python
        self._cmd_if = StreamIF(name=f"{self.name}_cmd_if", sim=self.sim, clk=self.clk,
                                bitwidth=w, framed=True)
        self._cmd_if.bind("master", self.seq.cmd_out)        # Sequencer -> MemRStream (framed command)
        self._cmd_if.bind("slave",  self.rstream.s_cmd)
        # ... _data_if (rstream.m_out -> wstream.s_in, also framed=True) the same way ...
        for i in (self._cmd_if, self._data_if):
            self.add_if(i)
```

**3. Name the boundary ports.** That is the entire third step:

```python
        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
```

Everything else about the graph is *derived from what you already wrote*, because declaring it twice is
how two descriptions drift apart:

| what the generator needs                           | where it comes from                                                                                                        |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| which children become `hls::task`s, in what order | `add_comp` order                                                                                                         |
| the internal FIFOs and their C++ names             | the `add_if` interfaces — each one *is* an edge, named after itself                                                    |
| how each edge lowers                               | the interface's **type/flags** (`StreamIF` → `hls::stream`; `StreamIF(framed=True)` → a `framed_word` FIFO) |
| which endpoints are boundary ports, in what order  | any child endpoint*not* bound to an internal interface, in `add_comp` × `add_endpoint` order                        |
| each port's direction                              | the endpoint's **type** (`StreamIFSlave` → input, `MMIFWriteMaster` → written `m_axi`)                        |
| the `gmem` bundle assignment                      | policy, applied in boundary order —`m_in` → gmem0, `m_out` → gmem1                                                  |

Only the *names* are yours to say, and only because they cannot be derived: both `MemRStream` and
`MemWStream` call their AXI port `m_mem`, so the top's `m_in` / `m_out` have to be stated.

That is the whole composite: three children, two framed wires, four port names, and no body of its own.
Running it in Python — the `MemCopy` graph plus a testbench — is the concurrent simulation, and walking
that same graph is how the generated kernel is built.

## Next

[Testbench (Python)](./testbench.md) — the graph that surrounds this design, and running it in pysim.
Then [DUT codegen](./codegen_dut.md) — how this graph becomes the `ap_ctrl_none` top, and why every
task body here is hand-written.
