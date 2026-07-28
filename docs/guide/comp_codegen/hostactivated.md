---
title: Host-activated kernel in HLS
parent: Module Code Generation
nav_order: 3
audience: hls
applies_to: [HostActivated]
api: [kernel_files_to_str, kernel_signature, VitisRegMapMMIFSlave, synthesizable]
summary: "How a HostActivated module is realized as a control_driven_kernel: an ap_ctrl_hs top whose s_axilite register block carries the application registers plus the Vitis control word, with on_start extracted as the kernel body and each @synthesizable hook emitted as a declaration plus a hand-written stub."
---

# Host-activated kernel in HLS

A [`HostActivated`](../flows/sequential.md) module lowers to a **`control_driven_kernel`**: one
`ap_ctrl_hs` top-level function that the host launches and waits on. It is the realization side of the
[sequential flow](../flows/sequential.md); how to *write* one is
[Writing it in Python](../flows/sequential_python.md).

## `ap_ctrl_hs` — the handshake that makes it a function

A kernel needs an answer to one question: **who starts it?** Vitis calls the answer the kernel's
*block-level control protocol*, and sets it on the `return` port.

`ap_ctrl_hs` is "handshake": the block carries **`ap_start`** ("go"), **`ap_done`** ("finished"), plus
`ap_idle` and `ap_ready`. The caller raises `ap_start`, the kernel runs to completion, raises
`ap_done`, and stops.

**That is what makes the kernel a function** — arguments in, one run, a return — and it is why the
sequential flow is the simple one: a testbench can *call* it, and Vitis builds the RTL co-simulation
harness for you. The [free-running](./freerunning.md) alternative gives that up.

Who raises `ap_start` is a *separate* question. Usually it is a host over AXI-Lite; it can also be
another block on a wire.

## The register block

`VitisRegMapMMIFSlave` puts the control behind an `s_axilite` adapter, and the generated top exposes
each register as a scalar port:

```cpp
void poly_state(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out,
    ap_uint<1>&  halted,
    ap_uint<8>&  error,
    ap_uint<16>& tx_id
) {
#pragma HLS INTERFACE axis      port=s_in
#pragma HLS INTERFACE axis      port=m_out
#pragma HLS INTERFACE s_axilite port=halted  bundle=control
#pragma HLS INTERFACE s_axilite port=error   bundle=control
#pragma HLS INTERFACE s_axilite port=tx_id   bundle=control
#pragma HLS INTERFACE s_axilite port=return  bundle=control
```

`port=return bundle=control` is the line that puts the `ap_ctrl_hs` handshake itself into the AXI-Lite
block, which is what lets a host start the kernel by writing a register rather than driving a pin.

You declare only the **application** registers. `VitisRegMap` adds the Vitis control block — `ap_start`
and `ap_done` share the control word at `0x00`, `0x04`/`0x08`/`0x0c` are the interrupt registers — so
application registers land at `0x10` onward at Vitis's 8-byte scalar stride. `ap_done` is managed for
you: cleared on `ap_start`, set when `on_start` returns.

A module with **no** regmap is still `ap_ctrl_hs` — its arguments simply arrive as ports rather than
registers, and its control is on raw pins. `block_scale` is one. This is easy to confuse with
free-running, because both extract a non-`on_start` method; the difference is the protocol, and raw
pins are still a handshake.

## `on_start` becomes the body

`on_start` is [extracted](./extractor.md) — read as source and translated statement by statement — and
returning from it is what raises `ap_done`.

```python
def on_start(self) -> ProcessGen[None]:          # <- extracted; becomes the kernel body
    y = self.compute(                             # <- a hook CALL is emitted...
        self.regmap.get("x"),
        self.regmap.get("a"),
        self.regmap.get("b"),
    )
    self.regmap.set("y", y)

@synthesizable
def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
    return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))   # <- ...but NOT this body
```

`self.regmap.get(...)` lowers to a read of the register port already in scope; `set` to a write. A
[`HwState`](../memory/hwstate.md) declaration becomes a `static` at the top of this function body.

## The generated file set

`kernel_files_to_str` emits `<kernel>.hpp` (the declaration plus schema includes), `<kernel>.cpp` (the
top above), and one stub per hook. Which files are framework-owned and rewritten versus sticky and
never overwritten is [Generated files](./codegen.md); the naming and namespace rules are there too.

## See also

- [Sequential (host-activated)](../flows/sequential.md) — the flow, its three gates, and the ladder
  from Python simulation to RTL co-simulation.
- [Writing it in Python](../flows/sequential_python.md) — the authoring side.
- [Endpoint interfaces](./interface.md) — how each endpoint becomes a port.
- [Module structure](./structure.md) — the contract, and which method is extracted for which kind.
