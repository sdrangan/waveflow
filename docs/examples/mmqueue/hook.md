---
title: Writing the kernel hook
parent: AXI-MM Command Queue (VMAC)
nav_order: 7
has_children: false
---

# Writing the kernel hook — `vmac_compute_impl.tpp`

`vmac_compute` is the one method whose C++ is hand-written, in
[`examples/vmac/vmac_compute_impl.tpp`](../../../examples/vmac/vmac_compute_impl.tpp).
This page walks **this specific datapath**. The *general* hook contract — the
templated signature, how a hook plugs into the generated kernel, the csynth gotchas
(scalar `*_core` args, `#pragma HLS INLINE`), and the `read_array_lane` /
`read_array_slice` calls — is the guide's job:
[Writing a hook](../../guide/custom_hooks/writing.md) and
[Kernel patterns](../../guide/custom_hooks/patterns.md) already use VMAC for exactly
that. Read those for the rules; this page is the VMAC-specific datapath they leave
out.

> Documented **as it currently is** — if the complex-typed cleanups tracked in the
> repo's VMAC plan land later, the `.tpp` will move, but the shape below is what is in
> the tree today.

## Shape: a scalar-arg core + a thin wrapper

Per the [csynth gotcha](../../guide/custom_hooks/writing.md#pass-scalars-to-a-core-function--not-a-struct-by-value),
the real work is in a `vmac_compute_core` taking **flat scalar arguments**, with a
struct-taking `vmac_compute(VmacCmd cmd, mem)` wrapper kept only for the csim
testbench. The core is templated on the structural widths and takes the command as
scalars — `op`, `reduce`, `n_rows`/`n_cols`, the three regions' `addr`/`row_stride`
pairs, and the alpha fields:

```cpp
template <int MEM_BW, int MEM_AWIDTH, int DATA_BW, int INT_BITS, int ACC_BW, int OUT_BW,
          bool Q_RND, bool O_SAT, int MAX_COLS>
void vmac_compute_core(ap_uint<MEM_BW>* mem,
                       int op, bool reduce, ap_uint<16> n_rows, ap_uint<16> n_cols,
                       ap_uint<MEM_AWIDTH> a_addr, ap_int<MEM_AWIDTH> a_rs,
                       ap_uint<MEM_AWIDTH> b_addr, ap_int<MEM_AWIDTH> b_rs,
                       ap_uint<MEM_AWIDTH> y_addr, ap_int<MEM_AWIDTH> y_rs,
                       bool al_direct, /*alpha imm*/ ..., ap_uint<MEM_AWIDTH> al_addr,
                       ap_int<MEM_AWIDTH> al_stride);
```

## The complex types

The datapath carries the [three formats](./arithmetic.md) as concrete C++ types,
picked off the generated array-utils value types and the structural widths:

- **operand** `CX = vmac_in_au::value_type` — `std::complex<ap_fixed<DATA_BW,
  INT_BITS, …>>`;
- **output** `CXO = vmac_out_au::value_type` — the `OUT_BW` complex type;
- **accumulator** `ACC_CX = std::complex<ap_fixed<ACC_BW, ACC_BW − 2·F_in>>` — wide,
  with `2·F_in` fractional bits (the product scale);
- **requantize target** `REQ_FX = ap_fixed<OUT_BW, OUT_INT, QMODE, OMODE>` — carrying
  the command's rounding/overflow modes.

Full precision is held in the `ACC_CX` value until a **single** `cx_requantize` lands
it in `CXO`.

## The fused read-compute-write loop

Operand rows are read with the `read_array_lane` lane loop (see
[kernel patterns](../../guide/custom_hooks/patterns.md)): each iteration pulls the
next `PF` complex columns for `A` (and `B`) at running word pointers and runs the
complex datapath across the lane, `#pragma HLS UNROLL`-ed over the `PF` lanes:

```cpp
for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
    CX r = /* cmult(alpha, a) | cmult(a, conj(b)) | cadd(a, b) */ ;   // complex_utils
    if (reduce) acc[j] = cwiden<ACC_CX>(cadd(acc[j], r));             // accumulate wide
    else        y_lane[k] = cx_requantize<CXO, REQ_FX>(r);            // non-reduce: requantize now
}
```

The arithmetic (`cmult` / `cadd` / `conj` / `cwiden` / `cx_requantize`) comes from
the shipped `complex_utils` header, not inline formulas. When `reduce` is set the
column accumulators `acc[j]` carry the running sum at full precision; a second pass
over the columns then `cx_requantize`s each accumulator and writes the single output
row back with `write_array_lane`.

## `ab_eq` — suppressing B's read

The signature study of the [timing](./timing.md) page lives in one runtime flag:

```cpp
const bool ab_eq = need_b && (a_addr == b_addr) && (a_rs == b_rs);
```

When `B` aliases `A` element-for-element (same base **and** stride), the kernel fills
`b_lane` from `a_lane` instead of issuing a second `m_axi` burst. `ab_eq` is a
*runtime* predicate, so HLS fixes **one static II** at the worst case (B-read
present) and only gates whether the B transaction *fires* — it does not reschedule
shorter. That is the whole "same latency, freed bus" result, made concrete in the
kernel.

## Running pointers

Addresses arrive in **element** coordinates (matching the Python
[`Region`](./pymodel.md#region--element-coordinates)); the kernel converts to word
pointers with `elem_to_word<PF>` (which checks PF-alignment) and advances them per
beat (`a_w += 1` per lane iteration) and per row (`a_row += a_rsw`). Any helper that
touches `mem` is `INLINE` so the `m_axi` reads bind to the top — again, the
[guide's gotcha 2](../../guide/custom_hooks/writing.md#pragma-hls-inline-so-maxi-binds-to-the-top).

## Validated against the golden

Because `vmac_compute`'s Python sibling is the bit-exact [`execute` golden](./pymodel.md),
the `.tpp` is checked against it in [csim/cosim](./rtlsim.md) for every config — the
hook can never silently drift from the model.
