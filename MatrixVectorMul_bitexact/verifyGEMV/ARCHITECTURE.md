# What the nine DUTs are

All nine wrap `xf::blas::gemv` from Vitis BLAS L1 **unmodified**.  Nothing here reimplements the
kernel; the tops only supply sizes and move data in and out.

Each one exists to close a specific claim that C-simulation alone could not settle.

## Shared structure

Every top takes plain arrays, builds the streams internally with the library's own data movers,
and is a `#pragma HLS DATAFLOW` region:

```
    p_A[M*N] ──gem2Stream──────► l_strA ─┐
                                         ├─► gemv ──► l_strY ──writeStream2Vec──► p_y[M]
    p_x[N]   ──vec2GemStream───► l_strX ─┘
```

`vec2GemStream` rather than a hand-rolled feed is **required**, not stylistic: `gemv` consumes
the vector once *per row*, so a singly-written `x` starves and the kernel blocks forever rather
than erroring.

The eight 5-argument tops are generated from one macro in `src/gemv_top.cpp` rather than copied —
they differ only in element type and size, and copies drift.

## Why these sizes

With `B = N/P` beats and `Delays` beats per chunk (**4** for float, **8** for double), the
library's reduction and a plain binary tree are the *same* reduction below **4 chunks**.  A DUT
with fewer cannot tell a correct model from one that has the structure wrong.

| DUT | M | N | P | beats | padded? | chunks | a full tree differs on |
|---|---|---|---|---|---|---|---|
| `f32` | 4 | 64 | 4 | 16 | no | 4 | 4/16 rows |
| `f32_wide` | 3 | 128 | 8 | 16 | no | 4 | 3/12 rows |
| `f32_pad` | 2 | 208 | 16 | 13 | **yes → 16** | 4 | 2/8 rows |
| `f64` | 2 | 64 | 2 | 32 | no | 4 (of 8) | 1/8 rows |

The last column is the one that matters: it is how many rows would go wrong if the model had the
reduction shape wrong.  A DUT where it is zero proves nothing about the structure — and `f32_pad`
was resized from `N=176` for exactly that reason, since 11 beats padded to 12 is only 3 chunks
and measured **0/8**.

The four non-float DUTs share `M=3, N=32, P=4`.  Sweeping the stream width there would add
co-simulation time and no information: `dot_dsp` accumulates in index order at any width.

## The float DUTs — `f32`, `f32_wide`, `f32_pad`

| | |
|---|---|
| element type | `float` |
| dispatch | `DotHelper<float, ...>` → **`dot_tree`** |

`dot_tree` is `mul` then `sum`, and `sum` is three stages that together are *not* a plain tree:

```
preProcess    BinarySum over each beat of P products     -> one value per beat   (a tree)
padding       pad the beat count to a multiple of Delays
postProcess   per chunk of Delays beats: BinarySum       (a tree)
              then  finalSum += chunkResult              (SEQUENTIAL)
```

`Delays` is `AdderDelay<float>::m_Delays` = **4**, the floating-point adder latency the design
pipelines around.  A property of the element type, not a user knob, and it changes the answer.

These three vary what is checked: `f32` is the baseline, `f32_wide` shows the stream width is
not special at 4, and `f32_pad` is the only one whose beat count is **not** a multiple of
`Delays` — so it is the only one where `padding()` actually pads.

## `f64` — double

Same `dot_tree` path, but `AdderDelay<double>` is **8**, so beats are grouped in eights and every
rounding happens at 53 bits instead of 24.

This DUT exists because the model *used to* hardcode `float32` while `adder_delays` already
answered 8 for `float64` — so asking for double returned plausible, silently wrong numbers.  Only
running double against the real kernel could have caught that.

## `ab` — the `alpha`/`beta` overload

| | |
|---|---|
| computes | `yr = alpha * (M x) + beta * y` |
| (alpha, beta) | 6 pairs, including `(1,0)`, `(0,1)` and `(1e-8, 1e8)` |

No new kernel — a composition of three shipped ones:

```
gemv(...)  ->  l_x     the 5-arg overload above
scal(...)  ->  l_y     l_y[j] = beta * y[j]                  (scal.hpp:65)
axpy(...)  ->  yr      yr[j]  = alpha * l_x[j] + l_y[j]      (axpy.hpp:71)
```

**`beta * y` is rounded to float32 in `scal` before `axpy` adds it.**  Evaluating
`alpha*dot + beta*y` in one wider expression and rounding once — the natural model — gives
different bits.

**`axpy`'s line is a fused-multiply-add candidate**, and this DUT exists to settle whether HLS
takes it.  C-simulation is compiled with `-ffp-contract=off` so that a csim-vs-cosim difference
isolates what the *synthesis tool* chose rather than what the C compiler did.

## `i32` and `u32` — the integer path

| | |
|---|---|
| element types | `int32_t` and `ap_uint<32>` |
| dispatch | `DotHelper<...>` → **`dot_dsp`** |

```
t_MacDataType l_res = 0;
for each beat:  for j in 0..P-1:  l_res += l_x[j] * l_y[j];
```

One accumulator, index order, no tree.  `t_MacDataType` cannot differ from `t_DataType` — the
parameter is exposed and documented but fails to compile inside `gemv.hpp:47` — so a long dot
product **wraps**, and the input data is chosen so some rows overflow and some do not.

`i32` and `u32` are fed **the same file**.  That is the point: `ap_uint<32>` and `int32_t` are
identical hardware and identical stored bits, and only the value they denote differs.  A model
that reads the accumulator as signed unconditionally returns the negative counterpart of the
right answer for the unsigned kernel — which is what the model used to do.

## `fixed` and `fix24` — the broken path

| | |
|---|---|
| element types | `ap_fixed<16,8>` and `ap_fixed<24,12>`, both `AP_TRN` / `AP_WRAP` (the defaults) |
| dispatch | `dot_dsp` |

The accumulator **is** the element format, so every `+=` narrows back and applies `AP_TRN` then
`AP_WRAP`:

```
product          exact  -- ap_fixed<W,I> * ap_fixed<W,I> is ap_fixed<2W,2I>
l_res + product  exact  -- the operator's return type is wide enough
assignment       LOSSY  -- narrowing applies Q, then O
```

**And the output is corrupted.**  `dot_dsp` ends with `p_res.write(l_res)`, a *numeric* conversion
to the stream's `ap_uint<W>` rather than a bit repack.  It truncates toward zero to the integer
part; the consumer unpacks that integer as a raw stored field.  Both DUTs are compared against
`gemv_fixed_as_shipped` — a model **of the bug** — and the defect changes 9/9 rows in each, so
neither would pass for a model that ignored it.

`fix24` is not a duplicate of `fixed`.  `WideType` gives each element a slot of `sizeof(T)*8`
bits, which for `ap_fixed<24,12>` is **32 bits in C-simulation** while the value is 24 bits wide;
under `__SYNTHESIS__` the container is exact.  If that difference leaked into the data path, csim
and cosim would disagree here and nowhere else — `ap_fixed<16,8>` is exactly two bytes, so the
question does not arise for it.

Data is fed and read as **stored integers** (the `.range()` field).  A decimal round-trip would
insert a rounding step between the test data and the arithmetic under test.

## Number formats at a glance

| DUT | in | accumulator | out |
|---|---|---|---|
| `f32`, `f32_wide`, `f32_pad` | `float` | `float`, tree + sequential chunks | `float` |
| `f64` | `double` | `double`, chunks of 8 | `double` |
| `ab` | `float` | as `f32`, then `scal` + `axpy` | `float` |
| `i32` | `int32_t` | `int32_t`, wraps | `int32_t` |
| `u32` | `ap_uint<32>` | `ap_uint<32>`, wraps | `ap_uint<32>` |
| `fixed` | `ap_fixed<16,8>` | same, narrowed per element | **corrupted on write** |
| `fix24` | `ap_fixed<24,12>` | same, narrowed per element | **corrupted on write** |

## Target

`xc7z020clg484-1` (Zynq-7020), 10 ns clock.  Chosen because it is small and common, not because
the design needs it; nothing here is device-specific.
