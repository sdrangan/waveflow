# Bit-exact Vitis L1 SSR FFT model — working plan

Working area for a bit-exact Python model of the **Vitis DSP L1 SSR FFT** (fixed point).
The goal: predict the hardware's *bits* from Python, with no Vitis run in the inner loop.

Companion to [`plans/fft_bit_exact.md`](../plans/fft_bit_exact.md) in the repo proper — that
file is the argument, this one is the working record.  Where they disagree, this one is newer.

| | |
|---|---|
| Status | **S1 DONE**; **S2 mostly done** — model bit-exact on 4/5 vectors; overflow-boundary case is a known, pinned gap. |
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

### The network — `wf_fft/fft.py`, bit-exact on 4 of 5 vectors

Decomposition for `L = R^2`, `n = n2 + R*n1`, `k = k1 + R*k2`:

    X[k1 + R k2] = sum_n2 W_R^{n2 k2} * ( W_L^{n2 k1} * ( sum_n1 x[n2 + R n1] W_R^{n1 k1} ) )

Three facts out of the source made this tractable:

* **The radix-4 DFT is exact.**  `W_R^k` for `R=4` is exactly `{1, -j, -1, +j}` (stored
  `±65536`), so the butterfly multiplies are sign flips and swaps — no rounding at all.
* **The adder tree is exact.**  It accumulates into `ap_fixed<W+1, I+1>`
  (`hls_ssr_fft_butterfly_traits.hpp:34-37`), which *is* `add_format`.  Growth, not loss.
* **So the rotation is the only lossy step** — one `complexMultiply` per sample per stage
  boundary.  The whole transform loses precision in exactly one place.

That accounts for the width exactly: `16,2` → 2 adder levels → `18,4` → first-stage rotation
growth (`COMPLEX_ROTATED_BIT_GROWTH = 1`) → `19,5` → 2 more levels → `21,7`, which equals
`in_W + log2(L) + 1`.

Verified on **five independent input vectors**, not one — identifying a structure on a single
case and declaring victory is how a fitted model passes.

| vector | result |
|---|---|
| ramp, pseudo-random, impulse, constant | **bit-exact, 0/32 each** |
| alternating extremes | **25/32 wrong — known gap** |

### ⚠️ The open gap: intermediate overflow

`v4` drives every input to ±full scale, so stage-1 sums land exactly on the `I=4` accumulator
boundary (±8) and the hardware's intermediate **wrapping** dominates.  The model returns the
mathematically correct spectrum (zero outside `k = 0, 8`); the library returns large wrap
artifacts (`±65536`, `-329472`).  So this is not a 1-LSB rounding subtlety — it is a different
overflow path, and it shows only at full scale.

Tried, and did **not** fix it: applying overflow per adder-tree level rather than once; keeping
the `W_4` rotation in exact integers so negating the most-negative value cannot wrap.

Next step is what the original notes prescribed: **per-stage goldens**.  Instrument the C++ to
dump each stage and compare stage by stage — end-to-end comparison cannot localise a wrap that
happened two stages back.  Pinned by `test_fft_bit_exact_at_overflow_boundary`
(`xfail(strict=True)`, so it announces itself the moment it starts passing).

### Remaining

* Localise the overflow path with per-stage goldens; retire the xfail.
* Then S3/S4 below — noting `cquantize` already exists and is validated.

Model **the arithmetic, not the parallelism**: SSR is a throughput/layout property, so a
sequential model is bit-identical to any SSR factor.  Needs no changes to `waveflow/` —
existing growth tracking (`cadd` +1 int bit, `cmult` → `2W+1`, `csum` → `+ceil(log2 N)`) *is*
`NO_SCALING`.

Golden: extend `cpp/` to instantiate the real FFT and dump its output for a fixed input vector.

**Pin the output order first.**  `SSR_FFT_NATURAL` vs `SSR_FFT_DIGIT_REVERSED_TRANSPOSED` is a
permutation, not arithmetic — it cannot cause a 1-LSB error but it can cause a total mismatch
that *looks* like one.  Settle it before debugging any value.

## S3 — `cquantize` + `cshift`

`waveflow/hw/complexfield.py` exports `cadd / csub / cmult / conj / csum` and **nothing lossy**.
`fixpoint.py` has `quantize` (`:180`) and `shift` (`:168`) for real `FixedField` only.  Add the
complex forms on the `csum` split-recombine pattern (`complexfield.py:369`), and extend
`examples/schemas/complex` to cover them.

Caution: `shift` is a **lossless point-move** (stored bits unchanged, format reinterpreted).
`SSR_FFT_SCALE` wants a lossy right-shift, so it is `cshift` *composed with* `cquantize`.

## S4 — the other two scaling modes, then widen

`SSR_FFT_SCALE` and `SSR_FFT_GROW_TO_MAX_WIDTH` (grow to 27, saturate → `OMode.AP_SAT`).
Then larger `L`, other radices.

## Open questions

* **`CONVERGENT_RND` has no Waveflow equivalent.**  `butterfly_rnd_mode` is threaded as a
  template parameter through five headers but nothing ever dispatches on its value, and the
  twiddle cast is hardcoded to the rounding variant regardless — so it appears inert in 2025.1.
  If a later release implements it, it needs a new `QMode`, not a workaround.
* **`ext_len` for the general case.**  Only `L=16, R=4` is known.
* **Version skew: retired.**  2023.1 vs 2025.1 differ in all 45 fixed-FFT headers, but every
  difference is a copyright line or trailing whitespace.  Functionally identical.  Re-run the
  diff if the toolchain moves.
