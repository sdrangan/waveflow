# Bit-exact Vitis L1 SSR FFT model — working plan

Working area for a bit-exact Python model of the **Vitis DSP L1 SSR FFT** (fixed point).
The goal: predict the hardware's *bits* from Python, with no Vitis run in the inner loop.

Companion to [`plans/fft_bit_exact.md`](../plans/fft_bit_exact.md) in the repo proper — that
file is the argument, this one is the working record.  Where they disagree, this one is newer.

| | |
|---|---|
| Status | **S1–S5 DONE.** Bit-exact for any `L = 4^S` — verified at 16, 64 and **1024 against synthesized RTL**. S6 (other radices, forked sizes) remains. |
| Waveflow | `main` @ `e360b74` |
| Vitis | 2025.1 (`/tools/Xilinx/2025.1`) |
| Vitis DSP source | `/home/marco/AmirProjects/Vitis_Libraries_2025.1` (`2025.1` = `v2025.1_update2`) |

## The method, and why it is shaped this way

**Golden files come from Vitis's own code, never from a reimplementation.**  The C++ generator
in `cpp/` instantiates `xf::dsp::fft::TwiddleTable<>` from the shipped library and dumps what
*that* produces.  A golden built by re-deriving the algorithm in C++ would only prove the two
reimplementations agree with each other.

**Goldens are raw stored integers, not floats.**  Bit-exactness is a claim about stored bits.
A `double` round-trip can absorb exactly the 1-LSB error these stages exist to catch, so
`ap_fixed::range().to_int64()` is dumped and compared as an integer.  (Values are the *unsigned*
`W`-bit two's-complement pattern — `-1` in `ap_fixed<18,2>` reads as `196608`, not `-65536`.)

**Golden generation needs no Vitis run.**  `<ap_fixed.h>` compiles natively under g++, so the
generator is a millisecond binary rather than a csim.  Goldens are checked in, so the tests
need neither Vitis nor a compiler — only regeneration does.

**Every stage must be able to fail.**  A conformance test that passes for the wrong reason is
worse than none.  Each stage carries at least one test asserting that the *plausible wrong
model* produces a different answer — see `test_truncation_would_be_wrong`.

## Layout

```
fft_bitexact/
  PLAN.md                  this file
  cpp/dump_twiddle.cpp     golden generator -- instantiates Vitis's TwiddleTable
  tools/regen_golden.sh    compile + run it (override VITIS_INC / VLIB for other installs)
  wf_fft/twiddle.py        the Python model under test
  golden/*.json            checked-in goldens
  tests/test_twiddle.py    the S1 gate
```

Run: `pytest fft_bitexact/tests/`   Regenerate goldens: `./fft_bitexact/tools/regen_golden.sh`

## S1 — twiddle table  ✅ DONE

Match the FFT's twiddle constants for `L=16, R=4`, `ap_fixed<18,2>`.  Done in isolation
because every butterfly multiplies by a twiddle: one wrong constant yields a wrong FFT with no
obvious cause, and folding it into a full FFT means four suspects for any mismatch instead of
one.

**Result: bit-exact, 16/16, real and imaginary.**  `4 passed, 1 skipped`.

### What S1 found — the twiddles are rounded, not truncated

`plans/fft_bit_exact.md` originally said v1 could assume "truncation twiddles."  **That was
wrong**, and S1 exists to have caught it.  `TwiddleTable::initTwiddleTable`
(`hls_ssr_fft_twiddle_table.hpp:62-70`) casts through:

```cpp
typedef typename TwiddleTypeCastingTraits<...>::T_roundingBasedCastType casting_type;
p_table[i] = casting_type(real, imag);
```

`T_roundingBasedCastType` is `ap_fixed<W, I, AP_RND, AP_SAT>`.  It is used 11 times across 5
headers.  The `T_truncationBasedCastType` sitting beside it is declared 4 times and **never
used anywhere**.

So the correct quantization is **`AP_RND` + `AP_SAT`** — *not* `ap_fixed`'s defaults, which are
what an unsuspecting model would pick.  This is confirmed empirically, not just by reading:

| model | mismatches vs Vitis (of 16) |
|---|---|
| `AP_RND` / `AP_SAT` | **0 re, 0 im** |
| `AP_TRN` / `AP_WRAP` | 7 re, 7 im — wrong at 10 distinct indices |

Both modes exist in Waveflow, so nothing new was needed — but a model built on the obvious
guess would have been wrong at over half the table.

### Other S1 notes

* `EXTENDED_TWIDDLE_TALBE_LENGTH` = **16** for `L=16, R=4` (dumped from C++; the general
  formula is not yet modelled — `ext_len()` raises rather than guessing).
* `I = 2` integer bits is load-bearing: `ap_fixed<18,2>` stores `1.0` exactly as `2^16`.  With
  `I=1` every axis twiddle would saturate just short of 1.  Pinned by `test_axis_points_are_exact`.
* `imag = -sin(...)` negates in `double` **before** quantizing.  Under `AP_RND` (round half **up**, toward
  +inf -- `-0.5 lsb` -> `0`) that differs from quantize-then-negate.  `L=16` happens not to discriminate the two —
  the test skips, honestly, and says to revisit at larger `L`.

## S2 — sequential radix-4 model  ← IN PROGRESS

**Done:** golden captured, output ordering pinned, growth formulas extracted.
**Not done:** the bit-exact model itself.

The real `xf::dsp::fft::fft<>` compiles and runs **natively under g++** — `hls::stream` in csim
mode is an ordinary queue — so the S2 golden costs milliseconds, like S1's.  Config is the
default struct with `N=16, R=4`; input `ap_fixed<16,2>`, output `ap_fixed<21,7>`.
(`cpp/dump_fft.cpp` writes to a file, not stdout: the HLS csim runtime prints an
`INFO [HLS SIM]` line at exit that would corrupt JSON on stdout.)

### Ordering: pinned, no permutation needed

`SSR_FFT_NATURAL` output equals numpy's `fft` ordering — matched to `1.4e-05` relative
(quantization noise), while bit-reversed is off by `14.9`.  So a later bit-level failure can
never be explained away as "maybe the order is wrong".  Pinned by `test_output_is_natural_order`,
which also asserts the bit-reversed comparison *fails*, so the guard cannot go inert.

### ⚠️ NO_SCALING is **not** free — this plan was wrong

This document said the default config "needs no new arithmetic at all".  That holds for the
twiddle *table* (S1) but **not** for the transform.  Two formulas out of the library:

`FFTOutputTraits` (`hls_ssr_fft_output_traits.hpp:147-151`) — growth is bounded by `L`, not by
operand widths::

    OUTPUT_WL = inputSizeBits   + log2(L) + 1        # 16 + 4 + 1 = 21
    OUTPUT_IL = integerPartBits + log2(L) + 1        #  2 + 4 + 1 =  7

`FFTMultiplicationTraits` (`hls_ssr_fft_multiplication_traits.hpp:67-74`) — the twiddle product
is **max-of-formats, truncated and wrapped**, not full precision::

    product_IL = max(op1_IL, op2_IL)
    product_FL = max(op1_FL, op2_FL)
    typedef std::complex<ap_fixed<product_WL, product_IL, AP_TRN, AP_WRAP, 0>> T_productOpType;

For `ap_fixed<16,2>` data x `ap_fixed<18,2>` twiddles that is `ap_fixed<18,2,AP_TRN,AP_WRAP>` —
whereas Waveflow's `cmult` yields full precision `(2W+1, 2I+1) = (33,5)`.  **So the butterfly
must requantize after every multiply**, and `cquantize` is a prerequisite for S2, not S3 work.

Note the modes differ between the two places quantization happens: the twiddle *table* rounds
and saturates (`AP_RND`/`AP_SAT`, S1); the twiddle *product* truncates and wraps
(`AP_TRN`/`AP_WRAP`).  Using one where the other belongs is a 1-LSB error in the exact place
this plan says such errors hide.

### The two primitives: prototyped locally, validated, bit-exact

`wf_fft/cxquant.py`, against `golden/cxops_d16_2_t18_2.json` (from the library's own
`complexMultiply`, not a reimplementation).  Both **0 mismatches over 24 cases**.

**Nothing here reimplements quantization.**  `cquantize` splits re/im, calls
`fixputils.quantize` — the same function `fixpoint.quantize` uses, already conformance-tested
against real Vitis across all four `QMode` x `OMode` combinations by
`examples/schemas/fixedpoint` — and recombines, in the shape of `complexfield.csum`.

**`cmult` + one `cquantize` is the WRONG recipe.**  `complexMultiply`
(`hls_ssr_fft_complex_multiplier.hpp:29-45`) stores every partial product into `T_op1`, the
*first operand's* type, before combining::

    T_op1 real1 = op1.real() * op2.real();   // truncated into T_op1 here
    T_op1 real2 = op1.imag() * op2.imag();   // and here
    T_op1 real_out = real1 - real2;          // subtracted in T_op1
    p_product.real(real_out);                // then widened into T_prd

Three quantization points per component, not one.  Measured:

| model | wrong (of 24) |
|---|---|
| partial products truncated to `T_op1` (the library's) | **0 re, 0 im** |
| full-precision product then one requantize (the obvious one) | 18 re, 23 im |

So `complexfield.cmult` is the wrong primitive for this FFT even though it is the natural
reach.  `test_naive_full_product_would_be_wrong` pins the failure counts, so if anyone
"simplifies" `complex_multiply` back to the obvious form the tests say so.

This also lowers the stakes on promoting anything into `waveflow/`: `cquantize` is a thin,
validated composition, but `complex_multiply` is FFT-specific and probably should **not**
become a library primitive — it encodes this design's quantization points, not complex
arithmetic in general.

### The network — `wf_fft/fft.py`, **bit-exact on all 12 vectors**

Decomposition for `L = R^2`, `n = n2 + R*n1`, `k = k1 + R*k2`:

    X[k1 + R k2] = sum_n2 W_R^{n2 k2} * ( W_L^{n2 k1} * ( sum_n1 x[n2 + R n1] W_R^{n1 k1} ) )

**The formats were measured, not derived.**  Two rounds of reading the source and reasoning
about `ButterflyTraits` produced two different models, both wrong.  Instrumenting a copy of the
headers settled it in minutes.  Measured (`cpp/dump_stages.cpp`):

| | stage 1 (`isFirst`) | stage 2 |
|---|---|---|
| `bfly_in` | `(16,2)` | `(19,5)` |
| `bfly_prod` | `(17,3)` | `(20,5)` |
| `tree_lvl` | `(18,4)` | `(21,6)` |
| `bfly_out` | `(19,5)` | `(22,7)` → cast to `(21,7)` |

Three things that reading alone had got wrong: a radix-4 stage carries **two** accumulator
levels, so stage 1 emits `(19,5)` not `(18,4)`; the inter-stage rotation **preserves** the
format rather than widening it; and the internal `(22,7)` is cast down to the declared output
width at the end.

### The overflow rule — the last bit, and the subtlest

Tree additions **wrap at their OPERAND width and widen on assignment**, not at the accumulator
width the declared types suggest.  The decisive evidence, from the trace on the one value that
still differed:

    prod[2] + prod[3] = +524288        (both ap_fixed<20,5>)
    hardware stores    -524288         -> a wrap at 20 bits, the PRODUCT width
    yet the accumulator is declared (21,6), which holds +524288 comfortably

Wrapping at `(21,6)` leaves that value at `+524288` and the whole bin wrong.  This is invisible
on in-range data — it only bites when a sum crosses the operand boundary — which is exactly why
the first model looked perfect on four vectors and was wrong.

### Validation

Twelve vectors.  **Seven (v5–v11) were added *after* the rule was derived from v4**, so they
are confirmation rather than the cases the model was fitted to; five of the seven sit on or
across the accumulator boundary.  All bit-exact, 0/32 each.

Two teeth tests keep the gate honest: perturbing the twiddle table must break it, and wrapping
one bit wider (the natural misreading) must break it.  Both assert the patch actually fired, so
neither can go inert.

### Remaining

S2 is closed.  Next:

* **S4** — `SSR_FFT_SCALE` and `SSR_FFT_GROW_TO_MAX_WIDTH`, then larger `L` and other radices.
  `ext_len` and the `L = R^2` decomposition are the parts that generalise least; expect to
  re-measure rather than re-derive.

**Method note for whoever picks this up:** instrument first.  Two rounds of reasoning from the
headers produced two confidently wrong models; the tracer produced the right one immediately and
is reusable (`cpp/vendor_debug/` + `-DWF_FFT_TRACE`).

Model **the arithmetic, not the parallelism**: SSR is a throughput/layout property, so a
sequential model is bit-identical to any SSR factor.  Needs no changes to `waveflow/` —
existing growth tracking (`cadd` +1 int bit, `cmult` → `2W+1`, `csum` → `+ceil(log2 N)`) *is*
`NO_SCALING`.

Golden: extend `cpp/` to instantiate the real FFT and dump its output for a fixed input vector.

**Pin the output order first.**  `SSR_FFT_NATURAL` vs `SSR_FFT_DIGIT_REVERSED_TRANSPOSED` is a
permutation, not arithmetic — it cannot cause a 1-LSB error but it can cause a total mismatch
that *looks* like one.  Settle it before debugging any value.

## S3 — `cquantize`  ✅ DONE

`cquantize` is now `waveflow/hw/complexfield.py`, alongside `cadd / csub / cmult / conj / csum`
— the first lossy complex operation the library has.  It adds **no quantization logic**: it
splits re/im, calls `fixputils.quantize`, recombines, in the same shape as `csum`.

**Validated against Vitis, not asserted.**  `examples/schemas/complex` gained four `cquantize_*`
cases covering both `QMode` x both `OMode` on a wide->narrow conversion (`ap_fixed<24,8>` ->
`ap_fixed<12,4>`) — the direction the FFT butterfly needs.  The C++ side is the assignment
itself, `y[i] = a[i]` with differing declared in/out types, which is how a real design narrows a
value.  Suite: **51/51 bit-exact**, up from 47.

`fft_bitexact/wf_fft/cxquant.py` is now a thin shim over the library version, so there is one
implementation rather than two.

### What deliberately did NOT move

`complex_multiply` stays in `fft_bitexact/`.  It encodes *this design's* quantization points —
partial products truncated into `T_op1` before combining — not complex arithmetic in general.
Promoting it would put an FFT-shaped assumption in a general-purpose module, and the natural
reading of its name would be wrong: it is not "multiply two complex numbers".

`cshift` was also not added.  Nothing needs it yet: the FFT's `SSR_FFT_NO_SCALING` path never
shifts, and inventing an untested primitive for `SSR_FFT_SCALE` before S4 measures what that
mode actually does would repeat the mistake this plan already made twice.

### Hygiene

The change adds **zero** new mypy errors (file baseline 50, still 50 — the first draft added 12
by leaving `element_type` untyped) and leaves the repo suite unchanged at 3094 passed / 9
pre-existing failures.

## S4 — the other two scaling modes  ✅ DONE

All three `scaling_mode_enum` values are bit-exact: **0 mismatches over 36 FFT runs** (3 modes x
12 vectors).

Measured first, as S2 taught (`cpp/dump_modes.cpp` traces every declared width per mode):

| mode | stage 1 | stage 2 | out |
|---|---|---|---|
| `NO_SCALING` | `(16,2)→(19,5)` | `(19,5)→(22,7)` | cast to `(21,7)` |
| `SCALE` | `(16,2)→(16,5)` | `(16,5)→(16,7)` | `(16,7)` — **width never grows** |
| `GROW_TO_MAX_WIDTH` | `(16,2)→(19,5)` | `(19,5)→(21,7)` | `(21,7)`, no cast |

The integer part grows identically in all three — `+1` per accumulator level, plus one in the
first stage's rotation.  **The modes differ only in what happens to the width**, which is a
tidier statement than the guide's "grow / scale / grow-and-saturate" framing:

* `NO_SCALING` widens at every step, so nothing is discarded.
* `SCALE` holds the width fixed, so each level drops a fractional bit.  That *is* the per-stage
  right shift, expressed as a format rather than an explicit shift — there is no shift operation
  anywhere in the datapath.
* `GROW_TO_MAX_WIDTH` widens except in a non-first stage's rotation.

### The rule got simpler, not more special-cased

S2 ended with "wrap at the operand width".  S4 needed "drop a fractional bit", and rather than
branch per mode the two merged into one operation::

    def _accumulate(a, b, operand, target):
        wrapped = _apply_overflow(a + b, operand)      # what NO_SCALING needs
        return wrapped if operand == target else quantize(wrapped, operand, target)   # what SCALE needs

For `NO_SCALING` and `GROW` the convert keeps the fraction and is exact, so one rule serves all
three modes.  Getting there took one wrong turn worth recording: the first attempt paired level
2's addition with the *product* format instead of the level-1 output format, which broke the two
modes that had been passing.  The levels are `(prod → acc1)` then `(acc1 → acc2)`.

`GROW_TO_MAX_WIDTH`'s 27-bit cap is **not** modelled — it is not reached at these widths, so
there is nothing here to check it against.  A design near the cap needs a golden that reaches it
first.

### Teeth

Beyond the per-mode gates: one test asserts the three modes actually *differ* (so a bad regen
emitting one mode three times cannot make the parametrised gate vacuous), one asserts the mode
goldens share inputs with the S2 golden, and one asserts that modelling `SCALE` without the
fractional drop fails — the mistake that mode invites, and one the width check alone would miss.

## S5 — general `L = R^S`  ✅ DONE

`fft_general` is bit-exact for any `L = 4^S`:

| L | stages | checked against | result |
|---|---|---|---|
| 16 | 2 | Vitis golden, 12 vectors | **0 / 384** |
| 64 | 3 | Vitis golden, 6 vectors | **0 / 768** |
| **1024** | **5** | **C-sim and co-simulated RTL, 8 vectors** | **0 / 16384 each** |

The 1024 check reads the files `../verifyFFT1024` produced, so it is tied to real hardware
output rather than another golden made by the same helper.

The recursion was identified by feeding `x[n] = n` and reading the grouping out of the trace::

    A_q[m]     = sum_p x[m + p*(L/R)] * W_R^{p q}
    X[q + R u] = (L/R)-point DFT over m of ( A_q[m] * W_L^{m q} )

### Two things that only bite past L=16

Both were found by measurement after reasoning had produced a confident wrong answer, and both
are invisible at `L=16` — which is exactly why a single-size model looked finished.

**1. The twiddle table is a quarter wave.**  `EXTENDED_TWIDDLE_TALBE_LENGTH` is `L` at `L=16`
but `L/4` beyond it (16 entries at `L=64`, 256 at `L=1024`), and `readQuaterTwiddleTable`
rebuilds the circle from it: index symmetry, sign inversion, and an exact `-1` substituted at
`L/4` and `3L/4`.  A model that quantizes `cos`/`sin` directly reads indices the table does not
contain.  `twiddle.quarter_twiddles` reproduces it exactly (0 differences at L=16, 64 and 1024).

**2. The narrowing happens BEFORE the rotation, not inside it.**  A stage's output is cast to
the next stage's format first, and the twiddle multiply then runs with that narrow type as its
first operand.  Letting the multiply do the narrowing — the natural reading, since it takes a
product type — leaves 14 of 64 stage-3 inputs off by an LSB at `L=64`.  This was the last gap,
and it is pinned by `test_narrowing_after_the_stage_would_be_wrong`.

The index formula needed no change: tracing `index = n * p_k` showed stage 1 uses `m q` and
stage 2 uses `4 m q`, both in the full-`L` phase space — matching the `L/sub` scaling already in
the model.  Worth recording as *ruled out*, since it looked like the obvious suspect.

### What this cost, and the lesson repeating

Three wrong hypotheses were tested and discarded before the right one (index scaling; cast
ordering inside the multiply; quarter-wave alone).  Each took minutes because the tracer could
answer them directly; none would have been settled by re-reading the headers.  **Instrument
first** — for the third time in this plan, measurement beat derivation.

## S6 — other radices, and the forked sizes

Not started.  `R=2/8/16` change the butterfly matrix and tree depth.  Sizes where
`log2(L) % log2(R) != 0` (32, 128, 512 at `R=4`) take a different "forked" architecture that has
not been looked at.
