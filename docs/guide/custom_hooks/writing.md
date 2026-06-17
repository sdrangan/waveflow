---
title: Writing a hook
parent: Custom Hooks
nav_order: 1
audience: hls
api: [synthesizable]
summary: "How to write a custom-hook .tpp: the templated function signature codegen calls, how it plugs into the generated kernel (m_axi/stream ports + the generated packing it calls), and the two VMAC csynth gotchas — pass scalars to a *_core function (a by-value struct DCEs the kernel) and #pragma HLS INLINE so m_axi binds to the top."
---

# Writing a hook

A hook is a C++ function in the `impl_file` you named in `@synthesizable(impl_file="…")`. Codegen
emits a *call* to it from the generated kernel; you write the body. This page is the contract that
function must satisfy, walked through VMAC's
[`vmac_compute_impl.tpp`](../../../examples/vmac/vmac_compute_impl.tpp).

## The templated signature

The generated kernel is parameterized on the component's `HwParam` widths (see
[templating](../comp_codegen/templating.md)), so the hook is a **function template** over those same
widths, living in the component's `cpp_namespace`. VMAC's hook takes the command struct and the
`m_axi` memory pointer:

```cpp
namespace vmac_impl {   // == VmacAccel.cpp_namespace

template <int MEM_BW, int MEM_AWIDTH, int DATA_BW, int INT_BITS, int ACC_BW, int OUT_BW,
          bool Q_RND, bool O_SAT, int MAX_COLS>
void vmac_compute(VmacCmd cmd, ap_uint<MEM_BW>* mem) {
    // ... read operands, compute, write back ...
}

}  // namespace vmac_impl
```

The file is a `.tpp` (not `.cpp`) because it is templated: the definition must be visible at the
include site so the template instantiates for each width set. (The generated header includes it; see
[templating](../comp_codegen/templating.md) for why parameterized hook stubs become `.tpp`.)

## How it plugs in

The hook is handed the component's **ports** and works through the **generated packing helpers** — it
does not hand-roll bit twiddling:

- The `ap_uint<MEM_BW>* mem` argument is the component's `m_axi` port. The hook reads/writes it with
  the generated array-utils methods (`read_array_lane` / `read_array_slice` / `write_array_lane`) —
  the [kernel patterns](./patterns.md) page covers these.
- The command (`VmacCmd cmd`) is the deserialized control struct; its fields drive the addressing.
- Arithmetic comes from the generated/shipped headers the component pulls in (VMAC uses
  `complex_utils.hpp`), not from inline formulas.

So a hook is *glue + datapath*: it calls generated serialization to move typed data over the port,
and you write only the math in between.

## The csynth gotchas (from VMAC)

Two non-obvious rules, both learned from VMAC's `.tpp` and both about how the hook meets the
synthesizable top:

### 1. Pass scalars to a `*_core` function — not a struct by value

Passing the nested `VmacCmd` **struct by value** into the synthesizable path mis-decomposes through
HLS's array/struct optimization at csynth: loop bounds fold to 0 and the kernel is dead-code
eliminated. The fix is a `*_core` function that takes the command as **flat scalar arguments**
(each keeping its precise type), with the struct-taking wrapper kept only for the csim testbench:

```cpp
// Scalar-arg core — the synthesizable top calls THIS directly.
template <int MEM_BW, int MEM_AWIDTH, /* … widths … */>
void vmac_compute_core(
    ap_uint<MEM_BW>* mem,
    int op, bool reduce, ap_uint<16> n_rows, ap_uint<16> n_cols,
    ap_uint<MEM_AWIDTH> a_addr, ap_int<MEM_AWIDTH> a_rs, /* … */) {
#pragma HLS INLINE
    // ... datapath ...
}

// Thin struct-taking wrapper — fine for the csim harness; unwraps cmd into the core's scalars.
template <int MEM_BW, int MEM_AWIDTH, /* … */>
void vmac_compute(VmacCmd cmd, ap_uint<MEM_BW>* mem) {
    vmac_compute_core<MEM_BW, MEM_AWIDTH, /* … */>(
        mem, (int)cmd.op, (bool)cmd.reduce, cmd.n_rows, cmd.n_cols,
        cmd.a.addr, cmd.a.row_stride, /* … */);
}
```

Keeping the precise field types (`ap_uint<MEM_AWIDTH>` addresses, signed strides, `ap_uint<16>`
shape) also keeps the address arithmetic sized by `MEM_AWIDTH` rather than a stray 32-bit `int`.

### 2. `#pragma HLS INLINE` so `m_axi` binds to the top

The `*_core` function carries `#pragma HLS INLINE` so it is inlined **into the synthesizable top**.
That way the `m_axi` reads/writes belong to the top's `gmem` port. Left as a separate module, the
function would have "no outputs" and the `gmem` port would dangle. Any small helper that touches the
memory pointer (VMAC's `elem_to_word`) is `INLINE` for the same reason.

## Walkthrough: VMAC

Putting it together, `vmac_compute_core` is the whole VMAC datapath:

1. **Compute row addresses** from the command's element-coordinate addr/stride fields.
2. **Read operand rows** off `mem` with the generated lane methods (`vmac_in_au::read_array_lane`),
   and the per-row scalar with `read_array_slice` — see [kernel patterns](./patterns.md).
3. **Run the complex datapath** — `complex_utils` `cmult`/`cadd`/`conj`, an `ap_fixed` accumulator,
   one `cx_requantize` to the output format (full precision until the single requantize).
4. **Write results back** with `vmac_out_au::write_array_lane`.

Because the hook's Python sibling (`VmacAccel.execute`) is the bit-exact golden, the `.tpp` is
validated against it in csim — the hook can never silently drift from the model.

## See also

- [Kernel patterns](./patterns.md) — the port read/write calls and loop shapes a hook uses.
- [Component Code Generation: Templating](../comp_codegen/templating.md) — why the hook is a `.tpp` and how `HwParam` reaches it.
- [`@synthesizable`](../../../waveflow/hw/synth.py) — the decorator and its `impl_file` / `synth_fn` options.
