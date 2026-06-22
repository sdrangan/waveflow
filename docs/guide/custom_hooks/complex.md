---
title: Complex — data-dependent addressing
parent: Custom Hooks
nav_order: 4
audience: hls
api: [read_array_lane, write_array_lane, read_array_slice, synthesizable]
summary: "The complex hook pattern: the access pattern itself depends on runtime command fields (strided rows, per-row gather), so the datapath drives the m_axi port — a read_array_lane loop over a running word pointer. Walked through VMAC, with the two csynth gotchas (scalar *_core args; #pragma HLS INLINE so m_axi binds to the top)."
---

# Complex — data-dependent addressing

The [block](./block.md) pattern works when you can name the operand range up front. But some kernels
compute *where to read* from runtime command fields — strided matrix rows, a per-row gather, a
scatter whose stride is a register. There the load can't be hoisted into `run_proc`; the **datapath
itself** walks the `m_axi` port, address by address.

The worked example is VMAC
([`examples/vmac/vmac_compute_impl.tpp`](../../../examples/vmac/vmac_compute_impl.tpp)): a complex
vector engine that reads row-major operand matrices `A` (and `B`) at command-given base addresses and
row strides, runs a complex element-wise op with an optional reduction, and writes the result back —
all over one `m_axi` master.

## A running-pointer lane loop

The shape is the throughput [`read_array_lane`](./reference.md) loop over an `m_axi` pointer you
advance yourself: a word pointer per operand, bumped one word per beat (each beat is `PF` complex
columns) and by the row stride per row. The element→word conversion is a helper because the addresses
are runtime:

```cpp
// row bases / strides in *word* units (regions are PF-aligned; elem_to_word checks it)
const ap_uint<MEM_AWIDTH> a_w0 = elem_to_word<PF>(a_addr);
const ap_int<MEM_AWIDTH>  a_rsw = elem_to_word<PF>(a_rs);

ap_uint<MEM_AWIDTH> a_row = a_w0;
for (int i = 0; i < (int)n_rows; ++i) {
    ap_uint<MEM_AWIDTH> a_w = a_row;
    for (int col0 = 0; col0 < (int)n_cols; col0 += PF) {
#pragma HLS PIPELINE II=1
        const int cols = ((int)n_cols - col0 < PF) ? ((int)n_cols - col0) : PF;
        CX a_lane[PF];
#pragma HLS ARRAY_PARTITION variable=a_lane complete dim=1
        vmac_in_au::read_array_lane<MEM_BW>(mem + a_w, a_lane, cols);   // PF columns this beat
        // ... compute ...
        a_w += 1;                                                       // advance the word pointer
    }
    a_row += a_rsw;                                                     // advance by the row stride
}
```

`read_array_lane<MEM_BW>(mem + a_w, lane, cols)` reads the `PF` elements at the running pointer; you
own the pointer arithmetic (contrast the [stream](./stream.md) loop, which self-sequences). A single
arbitrary element — VMAC's per-row scalar `alpha[i]` at `al_addr + i*al_stride` — uses
[`read_array_slice`](./reference.md) instead (one element at a computed index, in element
coordinates). The result lane is written with `write_array_lane`.

## The complex datapath

The math is hand-written because it needs exact fixed-point control: `complex_utils`
`cmult`/`cadd`/`conj` on `std::complex<ap_fixed>` lanes, a **wide complex accumulator** carrying full
precision, and a **single** requantize to the output format at writeback:

```cpp
typedef ap_fixed<ACC_BW, ACC_BW - 2 * F_IN> ACC_FX;   // accumulator: frac depth 2*F_in, no rounding
typedef std::complex<ACC_FX> ACC_CX;
...
ACC_CX r = complex_utils::cwiden<ACC_CX>(complex_utils::cmult(a_lane[k], complex_utils::conj(b_lane[k])));
if (reduce) acc[j] = complex_utils::cwiden<ACC_CX>(complex_utils::cadd(acc[j], r));
else        y_lane[k] = complex_utils::cx_requantize<CXO, REQ_FX>(r);   // the one rounding
```

Full precision is held in the `ap_fixed` value until the single `cx_requantize`, so the `.tpp`
reproduces the Python golden (`VmacAccel.execute`) bit-for-bit. The kernel is **one fixed-format
datapath** configured at runtime by `op` / `reduce` (loop-invariant muxes, no II hit); the template
parameters are **widths only**, so every `ap_fixed` type is compile-time.

## The two csynth gotchas

Driving the port from the datapath surfaces two non-obvious rules — both about how the hook meets the
synthesizable top.

### 1. Pass scalars to a `*_core` function — not a struct by value

Passing the nested `VmacCmd` **struct by value** into the synthesizable path mis-decomposes through
HLS's array/struct optimization at csynth: loop bounds fold to 0 and the kernel is dead-code
eliminated. The fix is a `*_core` function that takes the command as **flat scalar arguments** (each
keeping its precise type), with the struct-taking wrapper kept only for the csim testbench:

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
memory pointer — VMAC's `elem_to_word` — is `INLINE` for the same reason. (This is the general
[`#pragma HLS INLINE`](./writing.md) rule; here the `m_axi` binding makes it load-bearing.)

## The hook is a `.tpp`

`vmac_compute` is a function template over the `HwParam` widths (`MEM_BW`, `DATA_BW`, …), so — like
every width-parameterized hook — it lives in a `.tpp` the generated header includes (see
[the `.cpp` vs `.tpp` rule](./writing.md#cpp-vs-tpp)).

## When to use it

Reach for the complex pattern when:

- the **access pattern depends on runtime command fields** — strided rows, per-row gather, a scatter
  whose stride is a register;
- you need **hand-tuned fixed-point** intermediates (a wide accumulator, a single requantize) that the
  extractor can't express;
- the operand is in memory (so it is `m_axi`, not a [stream](./stream.md)) but isn't a simple
  resident block (so not [block](./block.md)).

## See also

- [Kernel transfer reference](./reference.md) — `read_array_lane` / `read_array_slice` / `write_array_lane`.
- [Memory command queue](./queue.md) — a hook that is the synthesizable half of a transport interface.
- [`examples/vmac`](../../../examples/vmac/vmac_compute_impl.tpp) — the worked complex datapath.
