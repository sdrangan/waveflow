# `bram_toy` — a memory that cannot live inside the kernel

Two free-running tasks share a true-dual-port buffer: one writes at a running pointer, the other
answers addresses it is told. Vitis HLS has no way to express that *inside* a kernel — a local array
shared between two `hls::task` bodies silently becomes a synchronizing PIPO channel whose handshake
**stalls the writer**, and one `bram` port used both ways is a hard dataflow error. So the memory
lives beside the kernel as hand-written Verilog, and a generated wrapper joins them.

This example is the smallest complete instance of that shape, and it exists to be **gated against
something that already ran**: `plans/witness/t2p_bram/` is four hand-written files that were csynthed
and simulated before any of this infrastructure was designed.

```
bram_toy.py         the design: BramWrite + BramRead + T2pBram, and the TB graph
src/*.h             the two hand-written hls::task bodies (the witness's, plus a "buffer ready" token)
bram_toy_build.py   pysim -> codegen (top + memory + WRAPPER + XSI harness) -> csynth
xsi/bram_toy_top.v  THE WRAPPER — generated, and what xsim elaborates
```

## The scenario, and why it is a ramp

Write `buf[i] = i + 100` for 256 samples, then read addresses `0, 1, 7, 255, 128` and expect
`100, 101, 107, 355, 228`.

A **ramp rather than a constant, deliberately.** The likeliest failure is a read-latency mismatch
between the kernel's `#pragma HLS INTERFACE ... latency=N` and the memory's own read latency, which
shifts every value by one position — and sails through a constant check. Waveflow closes that by
construction: the memory's `bram_t2p.v` publishes `localparam READ_LATENCY = 1`, and the pragma is
emitted *from that number*. There is no latency field in Python to disagree with it.

## What each registry means

```python
self.add_comp(self.wr)       # -> an hls::task INSIDE the generated top
self.add_if(go_if)           # -> an hls::stream channel inside the top
self.add_rtl_mod(self.mem)   # -> a Verilog instance BESIDE the top, in the wrapper
self.add_rtl_if(w_if)        # -> a wrapper wire: the task's port stays a BOUNDARY port
```

The last line is the whole mechanism. Because a `BramIF` is *not* in the `add_if` registry,
`derive_boundary` never sees it, so `buf_w` and `buf_r` come out as boundary ports of the kernel with
no change to that walk at all — and the wrapper joins them to the memory one level up.

## Sequencing lives in the design, not the testbench

The witness's `tb.v` drove all 256 samples and *then* the addresses. A concurrent BFM harness cannot:
both `AxisMaster`s push from cycle 0, so a read of address 255 would land hundreds of cycles before
that word was written. The answer belongs in the design — the writer emits one "buffer ready" token
on an internal stream, and the reader waits for it once. That is also exactly the invariant the
memory asserts: `bram_t2p.v` `$error`s on a read-during-write collision, so a clean run is positive
evidence that *rd trails wr*.

## Build and run

```bash
python bram_toy_build.py --through codegen_tb    # no toolchain: pysim + all codegen
python bram_toy_build.py --through csynth        # needs Vitis HLS
pytest tests/examples/test_bram_toy_xsi.py -m xsi   # needs Vivado xsim + a prior csynth
```

The XSI gate elaborates **`bram_toy_top`** (the wrapper), not `bram_toy` — the memory is internal to
it, which is why the testbench sees only AXI-Stream and the BFM library needed no changes at all.
