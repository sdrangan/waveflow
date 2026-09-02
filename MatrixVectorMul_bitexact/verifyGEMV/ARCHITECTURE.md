# What the three DUTs are

All three wrap `xf::blas::gemv` from Vitis BLAS L1 unmodified.  Nothing here reimplements the
kernel; the tops only supply sizes and move data in and out.

## Shared structure

Each top takes plain arrays, builds the streams internally with the library's own data movers,
and is a `#pragma HLS DATAFLOW` region:

```
    p_A[M*N] ──gem2Stream──────► l_strA ─┐
                                         ├─► gemv ──► l_strY ──writeStream2Vec──► p_y[M]
    p_x[N]   ──vec2GemStream───► l_strX ─┘
```

`vec2GemStream` rather than a hand-rolled feed is **required**, not stylistic: `gemv` consumes
the vector once *per row*, so a singly-written `x` starves and the kernel blocks forever rather
than erroring.

The stream width is `2^t_LogParEntries` entries per beat.  On the float path this is part of the
numerical contract — it sets the reduction tree's shape — so it is fixed at compile time and
reported in every output header, and `verify.py` reads it from there rather than assuming it.

## `gemv_f32_top` — the reduction itself

| | |
|---|---|
| element type | `float` |
| M, N | 4, 64 |
| `t_LogParEntries` | 2 (P = 4) |
| dispatch | `DotHelper<float, ...>` → **`dot_tree`** |

`dot_tree` is `mul` then `sum`, and `sum` is three stages that together are *not* a plain tree:

```
preProcess    BinarySum over each beat of 4 products    -> 16 beat values   (a tree)
padding       pad the beat count to a multiple of Delays
postProcess   per chunk of Delays=4 beats: BinarySum    (a tree)
              then  finalSum += chunkResult             (SEQUENTIAL)
```

`Delays` is `AdderDelay<float>::m_Delays` = **4**, the floating-point adder latency the design
pipelines around.  It is a property of the element type, not a user knob, and it changes the
answer.

**Why N=64 and not something smaller.**  16 beats / 4 = **4 chunks**.  A left-fold and a balanced
tree agree up to three terms, so below 4 chunks the library's reduction and a plain full tree are
the *same* reduction and this DUT would pass for a model that had the structure wrong.  Four is
the smallest count at which the check has teeth.

## `gemv_ab_top` — the `alpha`/`beta` overload

| | |
|---|---|
| computes | `yr = alpha * (M x) + beta * y` |
| element type | `float` |
| M, N, P | 4, 64, 4 |
| (alpha, beta) | 6 pairs, including `(1,0)`, `(0,1)` and `(1e-8, 1e8)` |

No new kernel — a composition of three shipped ones:

```
gemv(...)  ->  l_x     the 5-arg overload above
scal(...)  ->  l_y     l_y[j] = beta * y[j]                  (scal.hpp:66)
axpy(...)  ->  yr      yr[j]  = alpha * l_x[j] + l_y[j]      (axpy.hpp:71)
```

Two things decide the bits.

**`beta * y` is rounded to float32 in `scal` before `axpy` adds it.**  Evaluating
`alpha*dot + beta*y` in one wider expression and rounding once — the natural model — gives
different bits.

**`axpy`'s line is a fused-multiply-add candidate.**  This DUT exists to settle whether HLS takes
it.  It does not: the synthesis report instantiates `fmul_32ns_32ns_32_4_max_dsp_1` and
`fadd_32ns_32ns_32_5_full_dsp_1` as separate cores, and the RTL matches the unfused model on all
96 rows while failing the fused one on the 4 rows that separate them.

The C-simulation is compiled with `-ffp-contract=off` (set in `run.tcl`) so that a csim-vs-cosim
difference would isolate what the *synthesis* tool chose rather than what the C compiler did.

## `gemv_fixed_top` — the broken one

| | |
|---|---|
| element type | `ap_fixed<16,8>` → `AP_TRN`, `AP_WRAP` (the template defaults) |
| M, N, P | 3, 32, 4 |
| dispatch | `DotHelper<ap_fixed, ...>` → **`dot_dsp`** |

`ap_fixed` is not `float`, so it takes `dot_dsp`: one accumulator, index order, no tree.

```
t_MacDataType l_res = 0;
for each beat:  for j in 0..3:  l_res += l_x[j] * l_y[j];
```

`t_MacDataType` cannot differ from `t_DataType` — the parameter is exposed and documented but
fails to compile inside `gemv.hpp:47` — so the accumulator **is** the element format.  Every
`+=` therefore narrows back to `<16,8>` and applies `AP_TRN` then `AP_WRAP`:

```
product          exact  -- ap_fixed<16,8> * ap_fixed<16,8> is ap_fixed<32,16>
l_res + product  exact  -- the operator's return type is wide enough
assignment       LOSSY  -- narrowing applies Q, then O
```

Consequently `parEntries` does **not** change the result on this path, unlike the float one.

**And the output is corrupted.**  `dot_dsp` ends with `p_res.write(l_res)`, a *numeric*
conversion to the stream's `ap_uint<16>` rather than a bit repack.  It truncates toward zero to
the integer part; the consumer unpacks that integer as a raw stored field.  The model compared
against here is `gemv_fixed_as_shipped` — a model of the bug — and it matches the RTL exactly.

Data is fed and read as **stored integers** (the `.range()` field).  A decimal round-trip would
insert a rounding step between the test data and the arithmetic under test.

## Number formats at a glance

| DUT | in | accumulator | out |
|---|---|---|---|
| `gemv_f32_top` | `float` | `float`, tree + sequential chunks | `float` |
| `gemv_ab_top` | `float` | as above, then `scal` + `axpy` | `float` |
| `gemv_fixed_top` | `ap_fixed<16,8>` | `ap_fixed<16,8>`, narrowed per element | `ap_fixed<16,8>` — **corrupted on write** |

## Target

`xc7z020clg484-1` (Zynq-7020), 10 ns clock.  Chosen because it is small and common, not because
the design needs it; nothing here is device-specific.
