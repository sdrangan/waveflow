# Bit-exact Vitis L1 SSR FFT model — the plan

**Status:** PLAN (2026-07-26).  Supersedes the feasibility notes in
[`fft_bit_exact_notes.md`](./fft_bit_exact_notes.md), which gated itself on "revisit once
`ComplexField` lands."  It has landed.  Everything below was checked against the source and
the toolchain on a Linux box with Vitis 2025.1 rather than reasoned about, and two of the
notes' three worries turn out not to be worries.

## What the notes were waiting for, and where it stands

Both prerequisites exist:

| prerequisite | where | state |
|---|---|---|
| `FixedField` | `waveflow/hw/fixpoint.py:33` | done |
| `ComplexField` | `waveflow/hw/complexfield.py:49` | done |
| the conformance rig to reuse | `examples/schemas/complex/` | done, **47/47 bit-exact** |

The rig was run end to end against Vitis 2025.1: `run_conformance` reports
`{"n_cases": 47, "n_exact": 47, "all_exact": true}` covering
`roundtrip / cmult / cadd / csub / conj` over signed and unsigned fixed, int s8/s16, and
float32/64.  It generates a Vitis kernel per case, csims it, diffs bit-for-bit, and names the
element that diverged.  That is the harness this plan validates against; it does not need to
be built.

Growth tracking is already correct and already matches what an unscaled FFT needs:

| op | `complexfield.py` | result format |
|---|---|---|
| `cadd` / `csub` | `:328` / `:338` | integer bits **+1** |
| `cmult` | `:348` | `(2W+1, 2I+1, signed)` |
| `csum` | `:369` | integer bits **+ceil(log2 N)** |

## The notes' two risk corners are both smaller than feared

The notes name twiddle quantization and per-stage round/saturate as "where 1-LSB divergence
hides."  Reading the shipped source (`Vitis_Libraries`, tag `2023.1_motor_libs_update`, at
`dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/`) narrows both.

**Rounding modes.**  Waveflow implements two of `ap_fixed`'s seven quantization modes
(`QMode.AP_TRN`, `QMode.AP_RND`) and two of its five overflow modes (`OMode.AP_WRAP`,
`OMode.AP_SAT`).  That looked like a hole.  It is not: every `ap_fixed` declaration under
`fixed/` uses only those four, at 59 / 59 / 4 / 4 occurrences respectively.  Nothing
instantiates `AP_TRN_ZERO`, `AP_RND_CONV`, `AP_SAT_SYM` or `AP_WRAP_SM`.

`butterfly_rnd_mode` does admit a second value —
`enum butterfly_rnd_mode_enum { TRN, CONVERGENT_RND }` (`hls_ssr_fft_enums.hpp:73`) — and
`CONVERGENT_RND` has **no** Waveflow equivalent.  It appears to be **inert in this version**:
the value is threaded as a template parameter through `hls_ssr_fft_types.hpp`,
`hls_ssr_fft_complex_exp_table.hpp` and `hls_ssr_fft_output_traits.hpp`, but *nothing
specializes on it* — there is no `TRN` vs `CONVERGENT_RND` dispatch anywhere under `fixed/`.
The two typedefs that would implement the choice,
`T_truncationBasedCastType` / `T_roundingBasedCastType`
(`hls_ssr_fft_twiddle_table_traits.hpp:158-159`), are **declared and never referenced**.
Outside those declarations the only mentions of `CONVERGENT_RND` under `fixed/` are two
Doxygen comment blocks in `hls_ssr_fft.hpp` (`:3292`, `:3365`).

So modelling `TRN` (the default, `hls_ssr_fft_enums.hpp:87`) should be sufficient *and* is
probably what the hardware does regardless of the setting.  Treat that as a version-specific
observation, not a guarantee — see version skew below.  It was nearly recorded here as
"`CONVERGENT_RND` is never referenced," which a truncated grep made look true; the conclusion
survived the correction but the reasoning did not.

**Twiddle quantization.**  The cast is exactly two typedefs
(`hls_ssr_fft_twiddle_table_traits.hpp:157-165`):

```cpp
typedef std::complex<ap_fixed<IL, FL> >                  T_truncationBasedCastType;  // AP_TRN + AP_WRAP
typedef std::complex<ap_fixed<IL, FL, AP_RND, AP_SAT> >  T_roundingBasedCastType;    // AP_RND + AP_SAT
```

Both land inside the supported set.  The notes' "18 bits (16F + 2I)" is confirmed, but as a
*default*, not a constant — it is `twiddle_table_word_length` / `twiddle_table_intger_part_length`
on the parameter struct.

## The gap the notes missed

The notes say the butterfly maps onto "`ComplexField.mult`/`add`/`quantize`."  The first two
exist.  **`quantize` does not, for complex.**  `waveflow/hw/fixpoint.py` has `quantize` (`:180`)
and `shift` (`:168`), both taking a real `FixedField` `DataArray`.  `complexfield.py` exports
`cadd / csub / cmult / conj / csum` and nothing lossy.

That blocks two of the three scaling modes and any non-`TRN` butterfly rounding.  It is the
real critical path, and it is not large: `csum` (`complexfield.py:369`) already establishes the
split-recombine pattern these follow —

```python
re, r = fixputils.fixed_sum(cx.re_of(v), fmt, axis=axis)
im, _ = fixputils.fixed_sum(cx.im_of(v), fmt, axis=axis)
return _wrap_complex(cx.make_complex(re, im, r), _result_inner(ea.kind, r))
```

One caution: `shift` is a **lossless point-move** (stored bits unchanged, format
reinterpreted).  `SSR_FFT_SCALE` wants a lossy right-shift, so it is `cshift` **composed with**
`cquantize` back to the working width, not `cshift` alone.  Anyone reading `shift`'s name and
assuming it discards LSBs will produce a model that is wrong in a way csim will catch late.

## v1 scope: the default parameter struct, and why

`ssr_fft_default_params` (`hls_ssr_fft_enums.hpp:75-88`, the `TRN` default at `:87`) is:

```cpp
static const int N = 1024;
static const int R = 4;
static const scaling_mode_enum      scaling_mode      = SSR_FFT_NO_SCALING;
static const fft_output_order_enum  output_data_order = SSR_FFT_NATURAL;
static const int twiddle_table_word_length        = 18;
static const int twiddle_table_intger_part_length = 2;   // "+1/-1 stored correctly"
static const transform_direction_enum transform_direction = FORWARD_TRANSFORM;
static const butterfly_rnd_mode_enum  butterfly_rnd_mode  = TRN;
```

Line these up against what Waveflow has **today**:

| default | today |
|---|---|
| `scaling_mode = SSR_FFT_NO_SCALING` | free — growth tracking already is this |
| `butterfly_rnd_mode = TRN` | `QMode.AP_TRN` |
| twiddle cast (truncation) | `AP_TRN` + `AP_WRAP` |
| `R = 4` | mechanical |

**The default configuration needs no new arithmetic at all.**  So v1 targets it exactly —
`R=4`, `SSR_FFT_NO_SCALING`, `TRN`, truncation twiddles, `SSR_FFT_NATURAL` — with `L = 16`
rather than 1024, small enough to diff by eye when a stage disagrees.

This deliberately inverts the notes' implied order.  The notes treat `cquantize` as
groundwork; sequencing it first spends effort on the two *non-default* scaling modes before
anything has proven the twiddle table matches — which is the thing most likely to be wrong.
Do the free configuration first and the riskiest question gets answered first.

## Stages

**S1 — twiddle table.**  Generate the `L=16, R=4` table in Python from `FixedField`
(W=18, I=2, `AP_TRN`, `AP_WRAP`), and diff against the table the header generates.  No FFT
yet.  This isolates risk corner #1 with nothing else in the frame.  If the constants match
here they will not surprise us later.

**S2 — sequential radix-4 model.**  Butterflies + digit-reversal in `cadd`/`csub`/`cmult`, no
scaling.  Model the **arithmetic, not the parallelism** — the notes' key simplification holds
and should be restated in code comments: SSR is a throughput/layout property, so a sequential
model is bit-identical to any SSR factor.  Golden = the Python model; assert against a real
Vitis FFT through the `examples/schemas/complex` rig.

**S3 — `cquantize` + `cshift`.**  Add them to `complexfield.py` on the `csum` pattern, extend
the complex conformance harness to cover them (new ops in `_OPS` and `kernels.py::_OPCALL`).
Now the arithmetic gap is closed, and closed with the same bit-exact evidence as everything
else in that file.

**S4 — the other two scaling modes.**  `SSR_FFT_SCALE` (right-shift `log2(R)`/stage =
`cshift` + `cquantize`) and `SSR_FFT_GROW_TO_MAX_WIDTH` (grow to 27 then saturate =
`OMode.AP_SAT`).  Then widen `L` and radix.

S1 and S2 need no changes to `waveflow/` at all.

## What could still go wrong

* **Version skew.**  The checked-out `Vitis_Libraries` is **2023.1**; the installed toolchain
  is **2025.1**.  The validation compares the Python model against whatever `vitis-run`
  compiles, so if the shipped FFT changed between those, the source being replicated is not
  the source being tested.  Settle this before trusting any bit-diff — either check out the
  matching tag or confirm the fixed FFT headers are unchanged across the two.
* **`CONVERGENT_RND` has no Waveflow equivalent.**  Believed inert in 2023.1 (nothing
  dispatches on it), so out of scope at the default — but "inert" is an observation about one
  checkout, and it is exactly the kind of thing that could be implemented in a later release.
  Re-check it under the toolchain actually being validated against.  If it is ever live, it
  needs a new `QMode`, not a workaround.
* **Digit-reversal / output order.**  `SSR_FFT_NATURAL` vs `SSR_FFT_DIGIT_REVERSED_TRANSPOSED`
  is a permutation, not arithmetic, so it cannot cause a 1-LSB divergence — but it can cause a
  total mismatch that *looks* like one.  Pin the order explicitly in S2 before debugging any
  value.

## Sources

- [L1 SSR FFT user guide (2020.2)](https://xilinx.github.io/Vitis_Libraries/dsp/2020.2/user_guide/L1.html)
- Local checkout: `/home/marco/AmirProjects/Vitis_Libraries` (`2023.1_motor_libs_update`),
  `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/`
- [Vitis_Libraries SSR FFT source](https://github.com/Xilinx/Vitis_Libraries/blob/master/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_traits.hpp)
