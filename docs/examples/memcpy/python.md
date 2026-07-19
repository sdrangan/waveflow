---
title: Python
parent: Memory Copy
nav_order: 2
has_children: true
---
# Python Model

This page builds the design in Python — the three [`FreeRunComp`](../../guide/flows/concurrent.md)
leaves and the composite that wires them — and shows *how to write each kind of block*. The whole design
is `examples/mem_copy/mem_copy.py`.

## The command and response structures

Every stream in the design carries a typed [schema](../../guide/schema/). Three matter here.

The **application command** the host sends on `s_cmd` is a `CopyCmd` — a source offset, a destination
offset, and a length, all element/word coordinates (not byte addresses):

```python
class CopyCmd(DataList):
    elements = {
        "src_off": {"schema": Word32, "description": "source element/word offset"},
        "dst_off": {"schema": Word32, "description": "destination element/word offset"},
        "n_words": {"schema": Word32, "description": "number of packed words to copy"},
        "tx_id":   {"schema": Word32, "description": "host transaction ID, echoed on completion"},
    }
```

(Four `Word32`s still pack into two 64-bit words at `MEM_DW=64`, so `tx_id` costs no extra command
words — it fills what was padding.)

The `Sequencer` turns each `CopyCmd` into two **internal commands** — an `MRCmd` for the reader and an
`MWCmd` for the writer (framework schemas from `waveflow/hw/mem_stream.py`). Besides the address and
length, each carries a small opaque array, `xfer_msg`, that the mem-streams pass through untouched and
echo back:

```python
class MRCmd(ParamSchema):          # (MWCmd is the mirror)
    elements = {
        "addr":     {"schema": Word32, ...},   # element offset to read from
        "len":      {"schema": Word32, ...},   # number of words
        "xfer_len": {"schema": Word32, ...},   # active length of xfer_msg
        "xfer_msg": {"schema": DataArray...},  # the opaque correlation cookie (default 8 words)
    }
```

Finally, the **response** the writer emits on `s_done` is a `MemComplete` — the word count transferred,
plus the same `xfer_msg`, echoed back unmodified:

```python
class MemComplete(ParamSchema):
    elements = {
        "len":      {"schema": Word32, ...},
        "xfer_len": {"schema": Word32, ...},
        "xfer_msg": {"schema": DataArray...},  # the command's cookie, echoed
    }
```

## Correlating a completion with its job

Because the stages are free-running, several jobs are in flight at once, so a completion on `s_done`
needs to say *which* job it belongs to. That is what `xfer_msg` is for: whatever the `Sequencer` stamps
into a command's cookie comes back in the matching `MemComplete`. The mem-streams never look inside it —
they only carry it — so the correlation scheme is the composing design's to choose.

Here the scheme is a **host transaction ID**: the host sets `tx_id` on each `CopyCmd`, and the
`Sequencer` copies it into the cookie, so a completion can be matched to the exact request the host
issued. Using the command's `tx_id` (rather than a counter the `Sequencer` invents) also means the
`Sequencer` holds **no cross-firing state** — which, as the next section shows, is what keeps its body
cleanly lowerable.

## Writing a leaf: the `Sequencer`

A leaf is a `FreeRunComp`. You do three things: declare its endpoints, implement `run_iter` (one
firing), and put the per-firing computation behind `@synthesizable` hooks.

**Endpoints** are declared in `__post_init__` — here one input stream and two output streams:

```python
class Sequencer(FreeRunComp):
    cpp_kernel_name: ClassVar[str | None] = "mem_seq"
    mem_dwidth: HwParam[int] = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cmd  = StreamIFSlave (name=f"{self.name}_s_cmd",  sim=self.sim, bitwidth=self.mem_dwidth)
        self.mr_cmd = StreamIFMaster(name=f"{self.name}_mr_cmd", sim=self.sim, bitwidth=self.mem_dwidth)
        self.mw_cmd = StreamIFMaster(name=f"{self.name}_mw_cmd", sim=self.sim, bitwidth=self.mem_dwidth)
        for ep in (self.s_cmd, self.mr_cmd, self.mw_cmd):
            self.add_endpoint(ep)
```

**`run_iter` is one firing** — the runtime re-fires it per job, so there is no command loop. Read a
command, build the two outputs, write them:

```python
    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get(CopyCmd)   # get one command
        msg = self.make_xfer_msg(cmd)              # the correlation cookie   (hook)
        mr  = self.make_mr_cmd(cmd, msg)           # build the read command   (hook)
        yield from self.mr_cmd.write(mr)           # write it
        mw  = self.make_mw_cmd(cmd, msg)           # build the write command  (hook)
        yield from self.mw_cmd.write(mw)           # write it
```

Notice `run_iter` is only **`get` → hook → `write`** — the actual work (the cookie, building each
command) is in `@synthesizable` methods, not inline. That shape is not stylistic; a rule of the code
generator forces it, and it is worth internalizing before you write your own leaf:

- **Constructing a `DataSchema` is not in the generator's vocabulary.** Building the cookie array and
  each command (`x = MRCmd(...)`) cannot be lowered, so they are hooks.

There is a second rule worth knowing even though it does not bite here: **a lowered body may not read
mutable `self.X`.** If the cookie were a running counter (as it was before `tx_id`), that counter would
be cross-firing state and could not appear in `run_iter` either. Sourcing the cookie from `cmd.tx_id`
sidesteps it — the `Sequencer` now keeps no state at all.

The hooks are the per-firing computation you write by hand:

```python
    @synthesizable
    def make_xfer_msg(self, cmd: CopyCmd) -> XferMsgArr:
        msg = np.zeros(self._xfer_msg_len, dtype=np.uint32)
        msg[0] = int(cmd.tx_id)                     # the host's transaction ID, echoed on completion
        return msg

    @synthesizable
    def make_mr_cmd(self, cmd: CopyCmd, msg: XferMsgArr) -> MRCmd:
        return MRCmd(addr=int(cmd.src_off), len=int(cmd.n_words), xfer_len=1, xfer_msg=msg)
```

`run_iter` **is** the pysim golden and the source of the generated task body; the `@synthesizable`
hooks are the parts codegen does *not* write — it emits their declarations and calls, and you supply the
C++ (a `// TODO` stub is written once). Keeping the Python hook and its C++ in agreement is your job;
only the tests check it. (The [next page](./codegen.md) shows exactly which files are generated.)

## The mem-stream leaves

`MemRStream` and `MemWStream` are also `FreeRunComp` leaves, but you do **not** write them — they are
framework components. Each owns an `m_axi` port (`m_mem`) and a data stream, and its body is a
hand-written, width-templated `hls::task` copied in by the build (a body that owns a memory port is not
lowered from `run_iter` — the dividing line is `m_axi`). `MemWStream` here is constructed with
`emit_done=True`, which adds the `s_done` port and the `MemComplete` echo. Their full story is
[Streaming Memory Kernels](../../guide/memory/memstream.md).

## Writing the composite: `MemCopy`

The composite is a `FreeRunComp` with sub-components instead of a `run_iter` body — that is what
"composite" means here. It has **no `run_iter`** — its children do the work; it only declares
structure. Three things, all in `__post_init__`:

**1. Add the sub-components** (insertion order is the generated `hls::task` order):

```python
class MemCopy(FreeRunComp):
    cpp_kernel_name: ClassVar[str | None] = "mem_copy"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.seq     = Sequencer (name=f"{self.name}_seq", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_r",   sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_w",   sim=self.sim, mem_dwidth=w,
                                  emit_done=True, clk=self.clk)
        for c in (self.seq, self.rstream, self.wstream):
            self.add_comp(c)
```

**2. Wire the internal streams** with `add_if` — each `StreamIF` binds a master endpoint on one child to
a slave on another, and becomes an on-chip FIFO in the generated top:

```python
        self._mr_if = StreamIF(name=f"{self.name}_mr_cmd_if", sim=self.sim, clk=self.clk, bitwidth=w)
        self._mr_if.bind("master", self.seq.mr_cmd)      # Sequencer -> MemRStream
        self._mr_if.bind("slave",  self.rstream.s_cmd)
        # ... mw_cmd (seq -> wstream) and copy_data (rstream.m_out -> wstream.s_in) the same way ...
        for i in (self._mr_if, self._mw_if, self._data_if):
            self.add_if(i)
```

**3. Name the boundary ports.** That is the entire third step:

```python
        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
```

Everything else about the graph is *derived from what you already wrote*, because declaring it twice is
how two descriptions drift apart:

| what the generator needs                           | where it comes from                                                                                                                                  |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| which children become`hls::task`s, in what order | `add_comp` order                                                                                                                                   |
| the internal FIFOs and their C++ names             | the`add_if` interfaces — each one *is* an edge, named after itself                                                                              |
| how each edge lowers                               | the interface's**type** (a `StreamIF` is an `hls::stream`; a `StreamOfBlocksIF` is a `stream_of_blocks` sized by its `element_type`) |
| which endpoints are boundary ports, in what order  | any child endpoint*not* bound to an internal interface, in `add_comp` × `add_endpoint` order                                                  |
| each port's direction                              | the endpoint's**type** (`StreamIFSlave` → input, `MMIFWriteMaster` → written `m_axi`)                                                  |
| the`gmem` bundle assignment                      | policy, applied in boundary order —`m_in` → gmem0, `m_out` → gmem1                                                                            |

Only the *names* are yours to say, and only because they cannot be derived: both `MemRStream` and
`MemWStream` call their AXI port `m_mem`, so the top's `m_in` / `m_out` have to be stated.

That is the whole composite: three children, three wires, four port names, and no body of its own.
Running it in Python — the `MemCopy` graph plus a testbench — is the concurrent simulation, and walking
that same graph is how the generated kernel is built.

## Next

[Kernel codegen](./codegen.md) — how this graph becomes the `ap_ctrl_none` top, and which task bodies
are generated versus hand-written.
