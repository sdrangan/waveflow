---
title: Code generation
parent: Shared memory between two modules
nav_order: 4
has_children: false
---

# Code generation

The same `BramSimple` class that ran in SimPy is the source three artifacts are generated from: the
Vitis HLS kernel, the memory placed beside it, and the **wrapper** that joins the two. This page
walks all three, and the one thing that has to be reconciled between them.

```bash
cd examples/bram_simple
python bram_simple_build.py --through csynth
```

## The messages' headers come first

Before anything else, the build emits one C++ header per message schema, from the *same* Python
declarations the model reads through:

```python
from examples.bram_simple.bram_simple import SCHEMA_CLASSES

for cls in SCHEMA_CLASSES:
    print(f"{cls.__name__:16s} -> include/{cls.include_filename}")
```

```
BramStatusEnumField -> include/bram_status.h
BramOpEnumField     -> include/bram_op.h
WriteComputeCmd     -> include/bram_write_compute_cmd.h
WriteResp           -> include/bram_write_resp.h
ReadCmd             -> include/bram_read_cmd.h
ReadResp            -> include/bram_read_resp.h
```

Each carries the struct, `nwords<W>()`, and the `read_stream<W>` / `write_stream<W>` pair the task
bodies use — so the field order and the widths have **one author**, and the kernel cannot disagree
with the model about them. `BramStatusField` is listed in its own right so the status arrives as a
real `enum class` rather than an integer literal:

```cpp
enum class BramStatus {
    OK = 0,
    OUT_OF_RANGE = 1,
};
```

`src/` is what a human wrote and `include/` is what the build produced; no message layout appears in
both. What *is* hand-written is the range check (`src/bram_cmd_range.h`) — that is the design's logic
rather than its message layout, and layout is the thing that must have exactly one author.

## The kernel

`render_top` walks the elaborated graph and emits a free-running `ap_ctrl_none` top. Every port,
every pragma and the one internal channel come from the graph; the task **bodies** do not — they are
hand-written headers this file includes.

```cpp
void bram_simple(
    hls::stream<ap_uint<64> >& cmd_w,
    hls::stream<ap_uint<64> >& data_w,
    ap_uint<64> buf_w[1024],
    hls::stream<ap_uint<64> >& resp_w,
    hls::stream<ap_uint<64> >& cmd_r,
    ap_uint<64> buf_r[1024],
    hls::stream<ap_uint<64> >& data_r,
    hls::stream<ap_uint<64> >& resp_r
) {
#pragma HLS INTERFACE axis port=cmd_w
#pragma HLS INTERFACE axis port=data_w
#pragma HLS INTERFACE mode=bram port=buf_w storage_type=ram_1p latency=1
#pragma HLS INTERFACE axis port=resp_w
#pragma HLS INTERFACE axis port=cmd_r
#pragma HLS INTERFACE mode=bram port=buf_r storage_type=ram_1wnr latency=1
#pragma HLS INTERFACE axis port=data_r
#pragma HLS INTERFACE axis port=resp_r
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<ap_uint<64> > go;
    #pragma HLS STREAM variable=go depth=1
    hls_thread_local hls::task t0(bram_write_compute_task<64, 1024>, buf_w, cmd_w, data_w, resp_w, go);
    hls_thread_local hls::task t1(bram_read_cmd_task<64, 1024>, buf_r, go, cmd_r, data_r, resp_r);
}
```

Four things to notice:

- **`ap_uint<64> buf_w[1024]` is a sized array, never a pointer.** `mode=bram` on an unsized pointer
  silently degrades to an `ap_vld` scalar port: no warning, a clean `csynth`, and a design elaborated
  against a memory that is not there. The size comes from `BramIFMaster.nelem`, and the element
  type from `BramIFMaster.element_type` — `ap_uint<64>` is `Word64` spelled in C++.
- **`latency=1` is the memory's own number.** It is emitted from `bram_t2p.v`'s
  `localparam READ_LATENCY = 1`, reached through the bound `BramIF` — so the pragma and the Verilog
  cannot be authored independently and therefore cannot desynchronize. Nothing in any Python file
  states it.
- **`storage_type` is derived, and the two ports differ.** `buf_r` is read-only and gets
  `ram_1wnr`; `buf_w` is declared
  [`access="readwrite"`](../../guide/interface/bram.md#accessreadwrite-and-the-storage_type-that-follows)
  — the `COMPUTE` opcode reads the words it rewrites — and gets `ram_1p`. The wrapper wires **one**
  physical memory port per declared `bram` port, and `ram_1wnr` would let Vitis take a second one it
  never wired. The visible price is that the in-place loop schedules at **II=2**: one port, a read
  and a write per element. That is the mechanism, not a limitation — see
  `plans/typed_transfer_codec.md` S5b for the measurement.
- **`ram_1p` changes the port list, and the wrapper follows.** A `ram_1wnr` port emits all fourteen
  signals (an A/B pair of seven); `ram_1p` emits only the A half, so `bram_simple_top.v`
  instantiates `buf_w` with seven pins and `buf_r` with fourteen. Naming a pin Vitis did not emit is
  an xvlog error, which is why the wrapper asks the port's own `storage_type` rather than assuming a
  pair.
- **`go` is an `hls::stream` inside the kernel**, at depth 1, and does not appear in the signature.
  That is the `add_if` registry doing its job.
- **The task bodies are includes.** They stay hand-written because they own a `bram` array parameter,
  which the extractor has no vocabulary for. Their Python twins are the pysim golden, never the
  source of the C++.

The port list and the pragmas come from the same `TopSpec` the wrapper is derived from:

```python
from waveflow.build.composite_gen import composite_top_spec
from waveflow.build.elaborate import elaborate
from examples.bram_simple.bram_simple import BramSimple

spec = composite_top_spec(elaborate(BramSimple, {"bitwidth": 64, "depth": 1024},
                                    name="bram_simple"), width=64)
for p in spec.ports:
    if p.kind == "bram":
        print(p.decl, "|", p.pragmas[0])
```

```
ap_uint<64> buf_w[1024] | #pragma HLS INTERFACE mode=bram port=buf_w storage_type=ram_1wnr latency=1
ap_uint<64> buf_r[1024] | #pragma HLS INTERFACE mode=bram port=buf_r storage_type=ram_1wnr latency=1
```

## The memory

`bram_t2p.v` is copied verbatim from `waveflow/build/rtl/` into the example's `xsi/` directory. It is
not generated and it is never rewritten — `DW` and `AW` ride on the *instantiation*, because a
Verilog parameter is not a code generator.

```verilog
    always @(posedge clk) begin
        if (a_en) begin
            if (|a_we) mem[a_addr[AW-1:0]] <= a_din;
            a_dout <= mem[a_addr[AW-1:0]];
        end
        if (b_en) begin
            if (|b_we) mem[b_addr[AW-1:0]] <= b_din;
            b_dout <= mem[b_addr[AW-1:0]];
        end
```

Port A is the write side and port B the read side, which is what the kernel's two *unidirectional*
`bram` interfaces expect — and it is the assignment the file's own `$error` assertion is written
against.

## The wrapper, and the convention it reconciles

`render_wrapper` emits a module that instantiates the kernel, instantiates each memory the graph
declares, and joins them. From outside it looks like a kernel with only its AXI-Stream ports.

```verilog
    bram_t2p #(.DW(64), .AW(10)) mem (
        .clk(ap_clk),
        .a_addr(buf_w_addr_a >> 3),
        .a_din(buf_w_din_a),
        .a_dout(buf_w_dout_a),
        .a_en(buf_w_en_a),
        .a_we(|buf_w_we_a),
        .b_addr(buf_r_addr_a >> 3),
        .b_din(buf_r_din_a),
        .b_dout(buf_r_dout_a),
        .b_en(buf_r_en_a),
        .b_we(|buf_r_we_a)
    );
```

Two of those connections are not pass-throughs, and both were bugs before they were features:

- **`>> 3` — the address.** Vitis addresses a `mode=bram` port in **bytes**; `bram_t2p` indexes
  **words**. Joining them straight through scales every address by the element's byte width, and
  everything past `depth / (W/8)` aliases onto a live word — silently.
- **`|we` — the write enable.** Vitis drives a byte-lane *mask*, one bit per byte of the word. The
  memory takes a single enable.

The shift is `log2(width / 8)`, and the emitter **refuses** a width whose scaling is not a shift
rather than guessing:

```python
from waveflow.build.hwcodegen import LoweringError
from waveflow.build.wrapper_gen import _bram_addr_shift

print([(w, _bram_addr_shift(w)) for w in (8, 16, 32, 64)])
try:
    _bram_addr_shift(24)
except LoweringError:
    print("24 bits: refused")
```

```
[(8, 0), (16, 1), (32, 2), (64, 3)]
24 bits: refused
```

The full convention — including why a design that never wraps will not notice if any of this is
wrong — is in [the interface guide](../../guide/interface/bram.md#the-addressing-convention).

The wrapper also has to deal with a **B half** it does not use. Vitis emits a full A/B pair per
`bram` interface whether the kernel uses both or not, so its unused outputs are left open and its
`Dout` — which is a kernel **input** — must be driven, or the elaboration carries an undriven `X`
into the design:

```verilog
    assign buf_w_dout_b = 64'd0;
    assign buf_r_dout_b = 64'd0;
```

## What a simulator elaborates is not the kernel

The wrapper is the top. `csynth`'s project, its report and its generated Verilog keep the *kernel's*
name; the `.f`, the snapshot and the shared library are named for the **wrapper**. One artifact keeps
the name it has, and the new one is visibly the outer layer.

```python
from waveflow.build.composite_gen import composite_top_spec, render_ports_h
from waveflow.build.elaborate import elaborate
from examples.bram_simple.bram_simple import BramSimple

spec = composite_top_spec(elaborate(BramSimple, {"bitwidth": 64, "depth": 1024},
                                    name="bram_simple"), width=64)
print(spec.top_name, "->", spec.elab_top)
h = render_ports_h(spec)
print("bram ports on the elaborated design:", "buf_w" in h or "buf_r" in h)
```

```
bram_simple -> bram_simple_top
bram ports on the elaborated design: False
```

A `bram` port is **not a pin** on the elaborated design — a testbench binding to it would be driving
a wire that does not exist on the module it loaded. That is why the RTL harness sees only AXI-Stream,
and why the BFM library needs no memory model.

## What `csynth` does not count {#what-csynth-does-not-count}

The synthesis report is for the kernel, and the memory is outside it:

```python
import xml.etree.ElementTree as ET
from pathlib import Path

rep = Path("examples/bram_simple/bram_simple_proj/solution1/syn/report/csynth.xml")
res = ET.parse(rep).find(".//AreaEstimates/Resources")
print("kernel BRAM_18K:", res.find("BRAM_18K").text)

from waveflow.hw.bram import ramb18_count
print("memory beside it:", ramb18_count(1024, 64), "RAMB18")
```

```
kernel BRAM_18K: 0
memory beside it: 4 RAMB18
```

**Zero, and it is not zero.** A resource estimate taken from `csynth` alone would miss the entire
memory. That is why `T2pBram` *declares* its footprint by geometry rather than waiting for a run —
depth × width maps to a primitive count by construction, and the alternative here is not a less
precise number, it is no number at all.

## The file list

The last thing `csynth` does is re-emit `rtl_bram_simple_top.f` from the RTL that is actually on
disk: every file `csynth` generated, then the memory, then the wrapper, in elaboration reading order.

```
../bram_simple_proj/solution1/syn/verilog/bram_simple.v
...
bram_t2p.v
bram_simple_top.v
```

Re-emitted rather than committed-and-trusted, because a stale file list plus a cached `xsimk.dll` is
exactly how an XSI run goes green while proving nothing — a renamed module leaves the `.f` naming a
file that no longer exists, and `xvlog` does not mind.

## See also

- [RTL simulation](rtlsim.md) — running what this page generated.
- [A module realized as Verilog](../../guide/comp_codegen/rtl_module.md) — `rtl_module()`, the
  port-name chain, and the latency single-source rule.
- [Free-running composite](../../guide/comp_codegen/freerunning_composite.md) — the generated top's
  shape, and the wrapper's place in it.
