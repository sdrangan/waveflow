---
title: Streaming Memory Kernels
parent: Memory Modeling
nav_order: 3
has_children: false
---

# Streaming Memory Kernels

The [`Memory`](./python.md) model above is the *host-side*, byte-addressed view used to build test
vectors and lay out buffers. This page covers the complementary *hardware-side* piece: two reusable,
free-running (`ap_ctrl_none`) HLS components — `MemRStream` and `MemWStream` — that stream a run of
words between an AXI memory port and an on-chip stream, plus `MemCopy`, a small composite that chains
them into a multi-task memcpy kernel whose top is generated from the component graph (the task bodies
themselves are hand-written — see [What generates, and what does not](#what-generates-and-what-does-not)).
All three live in [`examples/interleaver`](../../../examples/interleaver/) and
`waveflow/hw/mem_stream.py`.

## Why a separate memory model here

`MemRStream`/`MemWStream` are framework components (`waveflow.hw.mem_stream`), not example code — any
accelerator can compose them as its load/store stage. Their kernel body is **fixed**: a hand-validated
`hls::task` (the interleaver sandbox's `a2s`/`s2a`), parameterized only by the memory word width
(`MEM_DW`). Codegen for these two is a **template copy**, not a `run_iter` extraction — the canonical
C++ bodies live in `waveflow/build/mem_r_stream_task.h` / `mem_w_stream_task.h` and are copied verbatim
into each example's `include/` directory by `MemStreamStep`.

```python
# From waveflow/hw/mem_stream.py
@dataclass
class MemRStream(FreeRunComp):
    """The sole ``m_axi`` read owner: an MRCmd queue -> word-granular m_out burst."""
```

## The addressing convention: element coordinates, not byte addresses

Unlike `Memory`/`AddrUnit.byte`, every command these components accept carries a **word/element
coordinate** relative to a bound buffer base — not a byte address:

```python
# From waveflow/hw/mem_stream.py
Word32 = IntField.specialize(bitwidth=32, signed=False)   # word / element coordinate or count

class MRCmd(ParamSchema):
    elements = {
        "addr":      {"schema": Word32, "description": "element/word offset within the bound buffer"},
        "len":       {"schema": Word32, "description": "number of packed words to read"},
        ...
    }
```

The physical base address is set once via `bind_base()` — mirroring the `offset=slave` AXI register —
so every `MRCmd`/`MWCmd` afterward is base-relative and unit-agnostic. Because `m_mem` is already a
word pointer in the generated C++, an element-coordinate command needs **no** byte↔word conversion in
the kernel body (contrast with the `memmgr::byte_addr_to_word_index` step on the [Vitis
page](./vitis.md), which is a byte-addressed `m_axi` port).

```python
# From waveflow/hw/mem_stream.py (MemRStream.bind_base)
def bind_base(self, base: int = 0) -> None:
    """Set the bound buffer's physical base (the offset=slave register, host domain).
    ... the default 0 is the flat single-arena mode used by the sim harness and the BFM."""
    self._base = int(base)
```

## Word-granular streaming, overlapped read/write

Both components move data one `ap_uint<MEM_DW>` word per cycle — never element-granular — because
element-granular streams halve bus bandwidth once `MEM_DW > 32`. The pysim golden (`run_iter`) models
the hardware's overlapped burst: the read and the write of the same word happen the same cycle in RTL
(II=1), so a burst costs `~n_words + fill`, not `~2 * n_words`:

```python
# From waveflow/hw/mem_stream.py (MemRStream.run_iter)
region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
words, t0 = yield from region.read_slice_pipelined(w0, w0 + nw)
# early-anchor the output at the first-word-available time + pipeline fill: the read and
# write OVERLAP (write_pipelined shortens its wait when the anchor is already past).
yield from self.m_out.write_pipelined(words, t_out_start=t0 + self._fill)
```

## Completion echo: `MemComplete` and the `xfer_msg` correlation cookie

By default a `MemRStream`/`MemWStream` is a bare 3-argument kernel (`s_cmd`, `m_mem`, and its stream).
Setting `emit_done=True` adds a fourth `s_done` port and, after each burst, writes a `MemComplete`
struct — the word count transferred plus the command's `xfer_msg` cookie, echoed back unmodified — so
a downstream consumer can correlate a completion with the job that issued it:

```python
# From waveflow/hw/mem_stream.py
class MemComplete(ParamSchema):
    elements = {
        "len":       {"schema": Word32, "description": "number of words transferred"},
        "xfer_len":  {"schema": Word32, "description": "valid length of the echoed xfer_msg payload"},
        "xfer_msg":  {"schema": DataArray.specialize(element_type=Word32, max_shape=(max_xfer_len,)),
                      "description": "the command's xfer_msg, echoed back unmodified"},
    }
```

`xfer_msg` is an opaque, fixed-capacity array (`max_xfer_len` words, default 8) — the component never
interprets it, only carries it through. `MemCopy`'s `Sequencer` uses slot 0 as a per-job index. One
firing of the free-running loop is one command:

```python
# From examples/interleaver/mem_copy.py (Sequencer.run_iter)
cmd: CopyCmd = yield from self.s_cmd.get(CopyCmd)
msg = self.next_xfer_msg()
mr = self.make_mr_cmd(cmd, msg)
yield from self.mr_cmd.write(mr)
mw = self.make_mw_cmd(cmd, msg)
yield from self.mw_cmd.write(mw)
```

The per-job counter is deliberately **not** in `run_iter` — it lives behind the `@synthesizable`
`next_xfer_msg` hook. Two rules force that split, and they are worth knowing before you write your own
sequencer: a lowered body may not read mutable `self.X`, and constructing a `DataSchema` is not in the
extractor's vocabulary (so the commands are built in hooks too, not inline). A hook is a *declaration*
whose C++ you write by hand — which is exactly where the counter is allowed to live:

```cpp
// From waveflow/build/mem_seq_task.h — hand-written, not lowered from the Python above
static ap_uint<32> job_idx = 0;
...
xfer_msg.data[0] = job_idx;
...
++job_idx;
```

The `static` is what makes the stamping survive across firings: the `hls::task` runtime re-invokes the
body without resetting its frame. That correspondence between `run_iter` and the `.h` is **not
checked by anything** — see [What generates, and what does not](#what-generates-and-what-does-not).

`xfer_msg` is backed by `UInt32Array` (`ap_uint<32> data[8]`), which is **not** directly subscriptable
in C++ — always index through `.data[i]`, both when reading a command's cookie and when writing the
echoed one:

```cpp
// From waveflow/build/mem_w_stream_done_task.h
COPY_MSG: for (int i = 0; i < 8; ++i) {
#pragma HLS UNROLL
        comp.xfer_msg.data[i] = c.xfer_msg.data[i];
}
```

## `MemCopy`: a graph-derived top over hand-written bodies

`MemCopy` (`examples/interleaver/mem_copy.py`) chains a pure-stream `Sequencer` into `MemRStream` ->
`MemWStream` over two internal command FIFOs and one data FIFO, all `StreamIF`s declared with
`add_comp`/`add_if`. The split matters: the **top** is generated, the **task bodies** are not. The top
is **derived from that graph** — `composite_top_spec` walks the sub-components' `kernel_task()`
signatures and the interface graph and resolves each task argument to either a top-level port or an
internal `hls_thread_local` FIFO, rather than a hand-written template:

```python
# From examples/interleaver/mem_copy.py (MemCopy.__post_init__)
self.internal_edges = [
    StreamEdge("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd),
    StreamEdge("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd),
    StreamEdge("copy_data", self.rstream.m_out, self.wstream.s_in),
]
self.boundary = [
    ("s_cmd", self.seq.s_cmd, "axis_in", None),
    ("m_in", self.rstream.m_mem, "maxi_read", "gmem0"),
    ("m_out", self.wstream.m_mem, "maxi_write", "gmem1"),
    ("s_done", self.wstream.s_done, "axis_out", None),
]
```

`MemRStream`/`MemWStream`'s own `kernel_task()` picks the 3-arg or 4-arg (`emit_done`) fixed body:

```python
# From waveflow/hw/mem_stream.py (MemWStream.kernel_task)
def kernel_task(self) -> KernelTask:
    if self.emit_done:
        return KernelTask(
            "mem_w_stream_done_task", "mem_w_stream_done_task.h",
            ("s_cmd", "s_in", "m_mem", "s_done"), template_args=(int(self.mem_dwidth),))
    return KernelTask("mem_w_stream_task", "mem_w_stream_task.h", ("s_cmd", "s_in", "m_mem"),
                      template_args=(int(self.mem_dwidth),))
```

## What generates, and what does not

All three components are `FreeRunComp`s, but that declares an *execution model* — it does not mean
codegen writes their bodies. For a `MemCopy`-shaped design today:

| Piece | Where it comes from |
|---|---|
| The composite top (ports, FIFOs, task instantiation, pragmas) | **Generated** from the component/interface graph by `composite_top_spec` |
| Each task body (`mem_seq_task.h`, `mem_r_stream_task.h`, …) | **Hand-written**, copied verbatim by `MemStreamStep` |
| `run_iter` | The **pysim golden** — never lowered |

So a `FreeRunComp`'s Python earns its keep three ways — it is the functional golden, the graph the top
is derived from, and the timing model — but it does not produce the body. **Nothing mechanically ties
`run_iter` to its `.h`.** They are two independent implementations that agree only because tests check
each against the same expectations: the pysim golden on the Python side, XSI on the RTL side. Keeping
them in agreement is the author's job, and drift is silent — hand-written task headers have drifted
from their schema before. If you change one, change the other, and lean on the tests to prove it.

This is the interim state, not the destination. `free_running_kernel` is a declared target
(`waveflow/hw/codegen_targets.py`) that is **not implemented**: `IMPLEMENTED_TARGETS` is
`{control_driven_kernel, sequential_vitis_tb}`. `Sequencer.run_iter` is nevertheless written in the
shape the extractor accepts (`get` -> hook -> `write`) and does lower today as a leaf — pinned by
`test_sequencer_run_iter_is_extractable` — but the emitted code is not wired into the composite,
because it emits an `s_axilite`/`ap_ctrl_hs` top rather than `ap_ctrl_none`, and it emits
`streamutils::axi4s_word<W>` streams where these tasks take `hls::stream<ap_uint<W>>` +
`read_stream<W>`. Closing those two gaps is what would let a generated body replace `mem_seq_task.h`;
`test_sequencer_codegen_gaps_are_still_open` fails when either closes.

## Verifying via XSI, not Vitis C/RTL cosim

Because these kernels are free-running (`ap_ctrl_none`), Vitis's C/RTL cosim refuses them — cosim
requires an `ap_ctrl_hs`-style start/done handshake. Verification instead drives the elaborated RTL
directly through XSI (`xsim.dir/<top>/xsimk.dll`) with a hand-written, cycle-based AXI-MM + AXI-Stream
BFM testbench (`examples/interleaver/xsi/*.cpp`). See [XSI](../build/xsi.md) for the general harness
setup (`xvlog`/`xelab`/`run.bat`); the mem-stream testbenches (`mem_r_bfm_tb.cpp`, `mem_w_bfm_tb.cpp`,
`mem_copy_bfm_tb.cpp`) follow that pattern directly.

### Sending a multi-word command over `s_cmd`

At `MEM_DW=64`, `MRCmd`/`MWCmd` (`addr`, `len`, `xfer_len`, 8-word `xfer_msg`) serialize to 6 words —
too wide for a single-beat command write. The BFM holds the command as a vector and presents one word
per accepted beat, dropping `TVALID` only once every word has gone out:

```cpp
// From examples/interleaver/xsi/mem_r_bfm_tb.cpp
std::vector<uint64_t> cmd_words = {
    (uint64_t)word_index | ((uint64_t)(uint32_t)N << 32),
    0ULL,
    0ULL, 0ULL, 0ULL, 0ULL,
};
const int NCMDW = (int)cmd_words.size();
...
auto driveAll = [&]() {
    d.putW(P_cmd_data, (cmd_widx < NCMDW) ? cmd_words[cmd_widx] : 0);
    d.put1(P_cmd_valid, h_cmd_valid);
    ...
};
...
if (cmd_beat && cmd_widx < NCMDW) {
    ++cmd_widx;
    h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u;
}
```

A testbench written for an older, narrower command shape (one word, no `xfer_len`/`xfer_msg`) will
stall forever waiting for words the kernel's `read_stream` never receives — the kernel looks "hung"
with `TVALID` asserted and no progress, rather than failing outright.

### A stale `.f` file can mask a real elaboration failure

The `rtl_<top>.f` file listing Verilog sources for `xvlog`/`xelab` is hand-maintained and goes stale
whenever a Vitis HLS re-synth changes the RTL submodule set (for example, a newly `#pragma HLS UNROLL`'d
loop over an 8-element `xfer_msg` array adds its own `_Pipeline_VITIS_LOOP_*` and `_RAM_AUTO_1R1W`
modules). If `xelab` then fails to find a module, but a `xsimk.dll`/testbench executable from a prior
successful build is still on disk, re-running `run.bat` can print a misleadingly plausible result from
the stale cached artifacts — always check the `xelab errorlevel=` line explicitly rather than trusting
only the final PASSED/FAILED line. Regenerate the `.f` file from the actual contents of
`<top>_proj/solution1/syn/verilog/` after any re-synth:

```bash
for f in mem_copy_proj/solution1/syn/verilog/*.v; do echo "../$f"; done > xsi/rtl_mem_copy.f
```

## Design guidance

**Use `MemRStream`/`MemWStream`** when a kernel's load or store stage is a plain contiguous burst
between an `m_axi` port and a word stream — they are the sole `m_axi` owners so any hierarchy composing
them keeps every other internal edge stream-only (required: an `hls::task` cannot mix an `m_axi` burst
and a `stream_of_blocks` lock in the same free-running task).

**Use `emit_done=True` and a `xfer_msg` cookie** when a composite issues more than one in-flight job and
a downstream consumer needs to know *which* job a completion belongs to — the cookie is opaque on
purpose, so it can carry whatever correlation scheme the composing component chooses (a job index here;
a demux tag for a future scatter/gather variant).

**Verify via XSI, not `-m vitis` cosim**, for any free-running (`ap_ctrl_none`) kernel in this family —
cosim's start/done protocol cannot drive it.
