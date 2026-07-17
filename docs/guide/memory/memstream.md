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
them into a multi-task memcpy kernel whose top is generated from the component graph (which task
bodies are generated and which are hand-written is the subject of [What generates, and what does
not](#what-generates-and-what-does-not)).

`MemRStream`/`MemWStream` are framework (`waveflow/hw/mem_stream.py`); `MemCopy` is its own worked
example in [`examples/mem_copy`](../../../examples/mem_copy/), self-contained down to its own `xsi/`
testbench workspace.

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
# From examples/mem_copy/mem_copy.py (Sequencer.run_iter)
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
// From examples/mem_copy/mem_seq_next_xfer_msg_impl.cpp — the hand-written hook impl (not lowered)
static ap_uint<32> job_idx = 0;
...
msg.data[0] = job_idx;
...
++job_idx;
```

The counter lives in the **hook impl**, not the task body: `mem_seq_task.h` (the body) is *generated*
from `run_iter` and pragma-free, so it has no place for a `static`; the `@synthesizable next_xfer_msg`
hook is the hand-written declaration where one is allowed. The `static` is what makes the stamping
survive across firings — the `hls::task` runtime re-invokes the body without resetting its frame. That
the hook's C++ agrees with `next_xfer_msg`'s Python is **not checked by anything** — see [What
generates, and what does not](#what-generates-and-what-does-not).

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

`MemCopy` (`examples/mem_copy/mem_copy.py`) chains a pure-stream `Sequencer` into `MemRStream` ->
`MemWStream` over two internal command FIFOs and one data FIFO, all `StreamIF`s declared with
`add_comp`/`add_if`. The split matters: the **top** is generated, the **task bodies** are not. The top
is **derived from that graph** — `composite_top_spec` walks the sub-components' `kernel_task()`
signatures and the interface graph and resolves each task argument to either a top-level port or an
internal `hls_thread_local` FIFO, rather than a hand-written template:

```python
# From examples/mem_copy/mem_copy.py (MemCopy.__post_init__)
self.internal_edges = [
    StreamEdge("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd),
    StreamEdge("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd),
    StreamEdge("copy_data", self.rstream.m_out, self.wstream.s_in),
]
self.boundary = [
    ("s_cmd", self.seq.s_cmd),
    ("m_in", self.rstream.m_mem),
    ("m_out", self.wstream.m_mem),
    ("s_done", self.wstream.s_done),
]
```

A boundary entry is just `(name, endpoint)` — nothing else is the assembler's to state. The
port's **direction** (an AXIS input vs output, a read vs write `m_axi`) is the endpoint's *type*:
`m_in` is a `MMIFReadMaster`, `m_out` a `MMIFWriteMaster`, and `kind_of_endpoint` reads it off the
class. The **gmem bundle** is assigned by policy — `gmem0`, `gmem1`, … in declaration order
(`bundle_map`) — because the same `MemWStream.m_mem` is `gmem0` standalone and `gmem1` here, so it
cannot be a fact about the port. Restating either would only create a second place to disagree
(see `plans/endpoint_types_not_tags.md`).

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

All three components are `FreeRunComp`s, but that declares an *execution model* — it does not by
itself mean codegen writes their bodies. For a `MemCopy`-shaped design today:

| Piece | Where it comes from |
|---|---|
| The composite top (ports, FIFOs, task instantiation, pragmas) | **Generated** from the component/interface graph — `composite_top_spec` |
| `mem_seq_task.h` (the Sequencer's body) | **Generated** from `Sequencer.run_iter` — `TaskBodyStep` |
| `mem_r_stream_task.h`, `mem_w_stream_done_task.h` | **Hand-written**, copied verbatim by `MemStreamStep` — they own `m_axi`, which task-body emission refuses |
| The Sequencer's `@synthesizable` hooks | **Hand-written**, sticky at the example root — the generator writes a `TODO` stub once, only if absent |
| The testbench's port binding | **Generated** from the same `TopSpec` as the top's pragmas — `render_ports_h` |

So the line is not "tops generate, bodies don't". It is **`m_axi`**: a stream-only leaf's body lowers
from its `run_iter`; a body that owns a memory port does not, and stays hand-written. `MemRStream`'s
`run_iter` is a pysim golden and nothing else; `Sequencer`'s is the source of its C++.

**What is still yours to keep in agreement.** Two things, and they are different in kind:

- **The hooks.** Codegen derives the body's *structure* — the stream read, the calls, the writes, in
  `run_iter`'s order — not its leaf computation. `mem_seq_next_xfer_msg_impl.cpp` and friends are
  hand-written, and **nothing mechanically ties a hook's C++ to the Python above it**. Only the tests
  check that.
- **The m_axi bodies.** `mem_r_stream_task.h` and its `run_iter` are two independent implementations
  that agree only because tests check each against the same expectations — the pysim golden on the
  Python side, XSI on the RTL side. Drift is silent here; hand-written task headers have drifted from
  their schema before.

Anything *generated* — the top, `mem_seq_task.h`, the port binding, the testbench's command words —
cannot drift by construction, which is the point of generating it. Do not hand-edit those; the banner
on each says so, and a regenerate will overwrite you.

The `MemCopy` top's own target is **`composite_kernel`**, and it *is* implemented
(`waveflow/hw/codegen_targets.py`: `IMPLEMENTED_TARGETS` now carries `composite_kernel` and
`sequential_xsi_tb` alongside the two Flow-1 targets). There is a single free-running DUT target: a
leaf compiled as its own top is just the 1-task case of a composite, walked by the same
`composite_top_spec` — the earlier `free_running_kernel` / `composite_kernel` split was one product
under two names and collapsed with the `FreeRunComp` merge (`plans/one_component_two_flows.md`). So
`check(MemCopy, "composite_kernel")` runs that real generator and passes.

What is *not* built is a separate path: the **old extractor** emitting a `FreeRunComp` leaf as its own
`ap_ctrl_none` top. `kernel_files_to_str(Sequencer)` routes through the `control_driven` extractor and
emits an `ap_ctrl_hs` top, not a free-running one — but that never blocks `MemCopy`, whose task bodies
carry no interface pragmas at all (so `ap_ctrl_none` never applies to a body).
`test_sequencer_codegen_gaps_are_still_open` tracks that extractor gap, distinct from the graph path
`check(…, "composite_kernel")` validates.

## Verifying via XSI, not Vitis C/RTL cosim

Because these kernels are free-running (`ap_ctrl_none`), Vitis's C/RTL cosim refuses them — cosim
requires an `ap_ctrl_hs`-style start/done handshake. Verification instead drives the elaborated RTL
directly through XSI (`xsim.dir/<top>/xsimk.dll`) with a cycle-based AXI-MM + AXI-Stream BFM. See
[XSI](../build/xsi.md) for the harness setup (`xvlog`/`xelab`/`run.bat`).

Neither the protocol nor the cycle loop is written per testbench. The framework BFM library
`waveflow/build/xsi/xsi_bfm.h` (copied beside each example's testbench) supplies `AxiMmReadSlave`,
`AxiMmWriteSlave`, `AxisMaster`, `AxisSlave`, `FlatMemory`, and `XsiSim`. Each model implements
`sample` / `update` / `drive`, and that split is load-bearing rather than stylistic: a beat is decided
from values sampled **before** the rising edge and applied **after** it, so collapsing the phases
changes when a transfer is seen.

```cpp
// From waveflow/build/xsi/xsi_bfm.h — the phase order every model obeys.
sim.clock_low();
sample();          // read kernel outputs, latch beat flags (VALID && READY)
sim.clock_high();
update();          // apply this cycle's beats, advance FSMs
drive();           // present held values for the next cycle
```

Which models exist, which RTL port each one drives, and the fixed-N loop that steps them through those
phases are **generated** — by `tb_top_spec` + `render_tb_harness` into `mem_copy_tb_harness.h`, walked
from the *same* `MemCopyTB` component graph that runs the pysim golden (one statement, two backends).
So the testbench file itself (`examples/mem_copy/xsi/mem_copy_bfm_tb.cpp`, ~90 lines) is almost
entirely the **test** — the part a component graph cannot know: what to put in memory, and what to check
afterward.

```cpp
// From examples/mem_copy/xsi/mem_copy_bfm_tb.cpp — the testbench, minus the golden check.
mem_copy_tb::Harness h("mem_copy_bfm.wdb", cmd_words);   // GENERATED: models, phases, run loop
for (int j = 0; j < vec::NUM_CMDS; ++j)                  // the TEST's stimulus: a known pattern
    for (int i = 0; i < vec::N; ++i) h.mem[vec::SRC_W[j] + i] = known_word(j, i);
h.run(N_CYCLES);                                         // step the generated harness
// ... then: every DST region == its SRC region (a memcpy), and each MemComplete echoed its job index.
```

This is the same split as [`sequential_xsi_tb`](../flows/freerun_seq.md), Flow 2's testbench target:
a `CompositeComp` testbench graph (DUT + BFM participants, wired by interfaces) is walked to the
harness, exactly as the DUT graph is walked to the top. `check(MemCopyTB, "sequential_xsi_tb")` runs
that generator.

**The gates are real, and exact.** `pytest -m xsi` drives all four kernels through RTL and asserts
their cycle counts — `mem_r_stream` 158, `mem_w_stream` 176, `mem_copy` 2835, `interleaver_canon`
3469, each measured to *last completion* (the loop's fixed drain tail is reported separately, since
it is a testbench constant and not a property of the design). Exact, not bounds: a count that moves
is a real behaviour change worth a human look, and an inequality would absorb a regression silently.
Each gate regenerates `rtl_<top>.f` from the RTL on disk and deletes `xsim.dir/<top>` first — a
hand-stale file list plus a cached `xsimk.dll` is how an XSI run goes green while proving nothing.

Those numbers are also where the free-running pipeline becomes visible. `mem_copy` copies 16 jobs in
2835 cycles — **~177 cycles/job**, against **~176** for a *single* `mem_w_stream` write on its own.
The reads hide entirely behind the writes: per-job cost is `max(read, write)`, not `read + write`
(158 + 176 = 334). Nothing in the testbench asks for that. It falls out of `AxisMaster` offering the
next command the moment the kernel accepts the last one, so the Sequencer is already handling job
*j+1* while `MemWStream` is still storing job *j*. It is the whole reason the tasks are free-running.

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
