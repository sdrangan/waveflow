---
title: Custom Hooks
parent: Guide
nav_order: 8
has_children: true
audience: hls
api: [synthesizable, FunctionStmt]
summary: "The hand-written codegen path: when the HwStmt extractor can't lower a datapath, you attach a hand-written Vitis C++ kernel to a component method with @synthesizable(impl_file=…). Covers the boundary with auto-generated codegen, the .tpp contract, and the in-kernel port/loop patterns."
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
| Source | the method's Python body | a `.tpp` you write |
| Lowered by | the `HwStmt` [extractor](../comp_codegen/extractor.md) | nothing — codegen emits a *call* to your C++ |
| Use when | the body is in the synthesizable subset | the datapath needs hand-tuned HLS C++ |

## The mechanism: `@synthesizable(impl_file=…)`

A method decorated [`@synthesizable(impl_file="…")`](../../../waveflow/hw/synth.py) with no
`synth_fn` is a **stub**: the extractor does not lower its Python body — codegen instead emits a call
to a user-written C++ function living in `impl_file`. The method's Python body is still the
**simulation** model (it runs in [PySim](../sim/)); only its *C++* is hand-written. So the same
method is bit-exact-checked in Python and synthesized from your `.tpp`.

```python
from waveflow.hw.synth import synthesizable

class VmacAccel(HwComponent):
    @synthesizable(impl_file="vmac_compute_impl.tpp")
    def vmac_compute(self, cmd: VmacCmd, mem: np.ndarray) -> ProcessGen[DataArray]:
        return self.execute(cmd, mem)   # Python body == the golden; C++ is hand-written
        yield                            # unreachable — makes this a generator (ProcessGen)
```

(From [`examples/vmac/vmac.py`](../../../examples/vmac/vmac.py). `execute` is the bit-exact Python
golden; the C++ contract it must match is the hand-written
[`vmac_compute_impl.tpp`](../../../examples/vmac/vmac_compute_impl.tpp).)

## VMAC — the running example

The [VMAC accelerator](../../../examples/vmac/) is the worked hook throughout this section: a complex
vector engine whose `vmac_compute` hook reads operand rows from an `m_axi` memory image, runs a
complex datapath (`complex_utils` arithmetic, an `ap_fixed` accumulator, a single requantize), and
writes results back — all hand-written, bit-exact with the Python golden by construction.

## In this section

- [Writing a hook](./writing.md) — the templated `.tpp` signature, how it plugs into the generated kernel, and the csynth gotchas (scalar `*_core` args, `#pragma HLS INLINE`), walked through VMAC.
- [Kernel patterns](./patterns.md) — moving data over an `m_axi` / stream port inside a hook (`read_array_lane` / `read_array_slice` / `read_stream_lane`, `TLAST`) and the common loop shapes.

## See also

- [Component Code Generation](../comp_codegen/) — the auto-generated structure your hook plugs into.
- [Hardware Components](../components/) — declaring the component (ports, `HwParam`) the hook belongs to.
- [Serialization](../schema/hls/serialization.md) / [raw arrays](../vectorization/hls/raw.md) — the generated packing methods a hook calls.
