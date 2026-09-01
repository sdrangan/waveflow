# Bit-exact Vitis L1 SSR FFT model — working plan

Working area for a bit-exact Python model of the **Vitis DSP L1 SSR FFT** (fixed point).
The goal: predict the hardware's *bits* from Python, with no Vitis run in the inner loop.

Companion to [`plans/fft_bit_exact.md`](../plans/fft_bit_exact.md) in the repo proper — that
file is the argument, this one is the working record.  Where they disagree, this one is newer.

| | |
|---|---|
| Status | **S1 DONE** (2026-07-26).  S2 next. |
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

## S2 — sequential radix-4 model  ← NEXT

Butterflies + digit-reversal via `cadd`/`csub`/`cmult`, `SSR_FFT_NO_SCALING`, `L=16`, `R=4`.

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
