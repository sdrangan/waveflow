---
title: Custom Hooks
parent: Guide
nav_order: 8
has_children: true
audience: hls
api: [synthesizable, FunctionStmt]
summary: "The hand-written codegen path: when the HwStmt extractor can't lower a datapath, you attach a hand-written Vitis C++ kernel to a component method with @synthesizable. Covers the boundary with auto-generated codegen, the hook mechanism, and a decision guide over the three hook patterns (block / stream / complex)."
---

# Custom Hooks

Most of a component lowers to C++ automatically — [Component Code Generation](../comp_codegen/)
walks the synthesizable subset of your Python and emits the kernel structure. But some datapaths are
beyond what the extractor can lower: a tight complex MAC, a custom pipeline, anything where you want
hand-tuned HLS pragmas and exact `ap_fixed` intermediates. For those you write the kernel body
yourself — a **custom hook** — and Waveflow drops it into the generated kernel in place of extracted
code.

## Auto-generated vs. hand-written

This is the **hand-written** side of hardware generation; the **auto-generated** side is
[Component Code Generation](../comp_codegen/). The boundary is one decorator:

| | Auto-generated | Hand-written (here) |
|---|---|---|
| Source | the method's Python body | a `.cpp` / `.tpp` you write |
| Lowered by | the `HwStmt` [extractor](../comp_codegen/extractor.md) | nothing — codegen emits a *call* to your C++ |
| Use when | the body is in the synthesizable subset | the datapath needs hand-tuned HLS C++ |

## The mechanism: `@synthesizable`

A method decorated [`@synthesizable`](../../../waveflow/hw/synth.py) with no `synth_fn` is a
**stub**: the extractor does not lower its Python body — codegen instead emits a call to a
user-written C++ function. The method's Python body is still the **simulation** model (it runs in
[PySim](../sim/)); only its *C++* is hand-written. So the same method is bit-exact-checked in Python
and synthesized from your hand-written file.

```python
from waveflow.hw.synth import synthesizable

class SimpFun(HwModule):
    @synthesizable
    def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
        return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))   # == the golden
```

(From [`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py). The Python body is the
bit-exact golden; the C++ contract it must match is the hand-written
[`simp_fun_compute_impl.cpp`](../../../examples/regmap/simp_fun_compute_impl.cpp).) Codegen finds the
C++ by the `{kernel}_{method}_impl.{cpp,tpp}` convention, or you name it explicitly with
`@synthesizable(impl_file="…")`. [Writing a hook](./writing.md) is the full contract.

## Which pattern? A decision guide

The three hook patterns differ by **who moves the data** — and that is also the order of increasing
difficulty. Ask what your operand looks like:

| Your operand is… | Who moves the data | The hook body | Pattern |
|---|---|---|---|
| a **fixed-size block** you can name up front | codegen (auto-gen `read_array`/`write_array` in `run_proc`) | pure compute over a materialized C++ array | [Block](./block.md) |
| a **stream** that arrives incrementally | the hook's own lane loop | read loop + compute + write, with `TLAST`/framing | [Stream](./stream.md) |
| a **data-dependent memory region** (strided rows, gather/scatter) | the datapath itself, over the `m_axi` port | strided `read_array_lane` + the datapath | [Complex](./complex.md) |

Start at [Writing a hook](./writing.md) for the mechanism (it uses the simplest case — a scalar hook
with no data movement), then jump to the pattern that matches your operand.

## In this section

- [Writing a hook](./writing.md) — the `@synthesizable` contract, the namespace, the bit-exact Python sibling, `#pragma HLS INLINE`, and `.cpp` vs `.tpp`, walked through the scalar `simp_fun` hook.
- [Block — load, compute, store](./block.md) — auto-generated `read_array`/`write_array` I/O, a pure-compute hook. The array generalization of the regmap kernel.
- [Stream — process as you read](./stream.md) — the lane loop over an AXI-Stream port (`read_axi4_stream_lane` / `write_axi4_stream_lane`, `pf` lanes, `TLAST`).
- [Complex — data-dependent addressing](./complex.md) — driving the `m_axi` port from the datapath (`read_array_lane` with a running pointer), and the two VMAC csynth gotchas.
- [Memory command queue](./queue.md) — the advanced case: a hook that is the synthesizable half of a transport interface (the `queue_get` ring dequeue).
- [Kernel transfer reference](./reference.md) — the in-kernel transfer-call cheat sheet (`read_array_lane` / `read_array_slice` / stream variants) and the Python↔C++ mapping table.

## See also

- [Component Code Generation](../comp_codegen/) — the auto-generated structure your hook plugs into.
- [Hardware Modules](../flows/modules.md) — declaring the component (ports, `HwParam`) the hook belongs to.
- [Serialization](../schema/hls/serialization.md) / [raw arrays](../vectorization/hls/raw.md) — the generated packing methods a hook calls.
