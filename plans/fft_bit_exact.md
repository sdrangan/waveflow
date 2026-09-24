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
hides."  Reading the shipped source narrows both.  **All line numbers below are for the 2025.1 checkout**
(`Vitis_Libraries_2025.1`, branch `2025.1` = `v2025.1_update2`, at
`dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/`) — the release matching the installed
toolchain.  The 2023.1 tree is functionally identical but its added copyright line shifts
every citation here by one.

**Rounding modes.**  Waveflow implements two of `ap_fixed`'s seven quantization modes
(`QMode.AP_TRN`, `QMode.AP_RND`) and two of its five overflow modes (`OMode.AP_WRAP`,
`OMode.AP_SAT`).  That looked like a hole.  It is not: every `ap_fixed` declaration under
`fixed/` uses only those four, at 59 / 59 / 4 / 4 occurrences respectively.  Nothing
instantiates `AP_TRN_ZERO`, `AP_RND_CONV`, `AP_SAT_SYM` or `AP_WRAP_SM`.

`butterfly_rnd_mode` does admit a second value —
`enum butterfly_rnd_mode_enum { TRN, CONVERGENT_RND }` (`hls_ssr_fft_enums.hpp:74`) — and
`CONVERGENT_RND` has **no** Waveflow equivalent.  It appears to be **inert in this version**:
the value is threaded as a template parameter through `hls_ssr_fft_types.hpp`,
`hls_ssr_fft_complex_exp_table.hpp` and `hls_ssr_fft_output_traits.hpp`, but *nothing
specializes on it* — there is no `TRN` vs `CONVERGENT_RND` dispatch anywhere under `fixed/`.
The cast is not selected by it at all: `TwiddleTable::initTwiddleTable`
(`hls_ssr_fft_twiddle_table.hpp:62`) hardcodes `T_roundingBasedCastType`, which is used 11
times across 5 headers.  Its sibling `T_truncationBasedCastType`
(`hls_ssr_fft_twiddle_table_traits.hpp:159`, `:165`, `:171`, `:176`) is declared four times and
**never used anywhere**.
Outside those declarations the only mentions of `CONVERGENT_RND` under `fixed/` are two
Doxygen comment blocks in `hls_ssr_fft.hpp` (`:3293`, `:3366`).

So modelling `TRN` (the default, `hls_ssr_fft_enums.hpp:88`) should be sufficient *and* is
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

Only the **rounding** one is ever used (see above), so the twiddles are quantized with
`AP_RND` + `AP_SAT` — *not* `ap_fixed`'s defaults.  S1 confirmed this empirically: modelling
them with `AP_TRN`/`AP_WRAP` is wrong at 10 of 16 entries for `L=16`.  Both modes are in
Waveflow, so nothing new is needed — but the obvious guess is the wrong one.  The notes'
"18 bits (16F + 2I)" is confirmed, but as a *default*, not a constant — it is `twiddle_table_word_length` / `twiddle_table_intger_part_length`
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

One caution, since `shift` is a **lossless point-move** (stored bits unchanged, format
reinterpreted): anyone reading its name and assuming it discards LSBs will produce a model that
is wrong in a way csim catches late.

> **This paragraph originally continued "`SSR_FFT_SCALE` wants a lossy right-shift, so it is
> `cshift` composed with `cquantize`."  S4 measured the mode and there is no shift in the
> datapath at all** — `SCALE` holds the accumulator *width* fixed while the integer part grows,
> so each level drops a fractional bit as a consequence of the declared format.  `cshift` was
> never needed and never written.

## v1 scope: the default parameter struct, and why

`ssr_fft_default_params` (`hls_ssr_fft_enums.hpp:76-89`, the `TRN` default at `:88`) is:

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
| `scaling_mode = SSR_FFT_NO_SCALING` | growth tracking already is this |
| `butterfly_rnd_mode = TRN` | `QMode.AP_TRN` |
| twiddle cast (**rounding**, always) | `AP_RND` + `AP_SAT` |
| `R = 4` | mechanical |

So v1 targets the default exactly — `R=4`, `SSR_FFT_NO_SCALING`, `TRN` butterflies,
`AP_RND`/`AP_SAT` twiddles, `SSR_FFT_NATURAL` — with `L = 16` rather than 1024, small enough to
diff by eye when a stage disagrees.

> **This section originally concluded "the default configuration needs no new arithmetic at
> all."  That was wrong**, and it is left here corrected rather than deleted because it is the
> plan's most instructive mistake.  The table above compares *declared modes*, and every one of
> them does match — but matching modes says nothing about **where** quantization is applied.  The
> butterfly turned out to requantize after every twiddle multiply, into a type the library
> derives per stage, so S2 needed `cquantize` after all and `cmult` turned out to be the wrong
> primitive entirely.  A checklist of settings is not a model of a datapath.

The ordering still stands, for a different reason than the one originally given.  Doing the
default configuration first answers the riskiest question — does the twiddle table match — before
any effort goes into the two non-default scaling modes.

## Stages

**S1 — twiddle table.**  ✅ **DONE (2026-07-26) — bit-exact, 16/16.**  Implemented in
[`fft_bitexact/`](../fft_bitexact/) (`PLAN.md` there is the working record).  The golden comes
from instantiating Vitis's own `TwiddleTable` natively under g++ — no Vitis run — and is
compared as raw stored integers, never floats.  It corrected this plan: the quantization is
`AP_RND`/`AP_SAT`, not the truncation this document originally assumed, which S1 caught before
anything was built on top of it.  Exactly what isolating risk corner #1 was for.

**S2 — sequential radix-4 model.**  ✅ **DONE — bit-exact, 12 vectors at `L=16`.**  The key
simplification held: SSR is a throughput/layout property, so a sequential model is bit-identical
to any SSR factor.  Two predictions in this plan did **not**:

* *"butterflies in `cadd`/`csub`/`cmult`"* — `complexfield.cmult` is the **wrong primitive**.
  The library truncates each partial product into the first operand's type before combining, so
  a full-precision multiply plus one requantize is wrong in 18 of 24 real parts.
* the per-stage widths were derived twice from the headers and were wrong twice; instrumenting a
  copy of the headers settled them in minutes.

**S3 — `cquantize`.**  ✅ **DONE — promoted to `waveflow/hw/complexfield.py`, Vitis-validated.**
`examples/schemas/complex` gained four `cquantize_*` cases over both `QMode` x both `OMode`;
the suite runs 51/51 bit-exact.  **`cshift` was never added** — nothing needed it, and inventing
an untested primitive for a mode S4 had not yet measured would have repeated the S2 mistake.

**S4 — the other two scaling modes.**  ✅ **DONE — all three modes bit-exact, 36 runs.**  This
plan predicted `SSR_FFT_SCALE` = *"right-shift `log2(R)`/stage = `cshift` + `cquantize`"*.
Measurement says otherwise: **there is no shift operation in the datapath at all.**  All three
modes grow the integer part identically and differ only in what happens to the *width*; `SCALE`
holds the width fixed, so each accumulator level drops a fractional bit.  The shift is a
consequence of a format rule, not an operation.

**S5 — general `L = R^S`.**  ✅ **DONE — bit-exact at `L=16`, `64`, and `1024`**, the last
against both C-simulation and co-simulated RTL (16384 values each).  Two behaviours appear only
past `L=16`: the twiddle table is a **quarter wave** beyond that size (`L/4` entries, rebuilt by
`readQuaterTwiddleTable` with an exact `-1` at `L/4` and `3L/4`), and a stage's output is
**narrowed before** the twiddle rotation rather than inside the multiply.

**S6 — other radices and the forked sizes.**  Not started.  `R=2/8/16` change the butterfly
matrix and tree depth; sizes where `log2(L) % log2(R) != 0` (32, 128, 512 at `R=4`) take a
different architecture that has not been examined.

The working record, with the measurements behind each of these, is
[`fft_bitexact/PLAN.md`](../fft_bitexact/PLAN.md); the runnable end-to-end checks are
[`fft_bitexact/VERIFY.md`](../fft_bitexact/VERIFY.md).

S1 and S2 needed no changes to `waveflow/` at all; S3 added exactly one function.

## What could still go wrong

* ~~**Version skew.**~~  **Retired (2026-07-26) — checked, not a risk.**  `2025.1`
  (`v2025.1_update2`, `b2c657d`) was checked out alongside the existing 2023.1 tree and the
  whole of `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/` diffed: all 45 files differ, and
  **every difference is a copyright header or trailing whitespace**.  Filtering those leaves
  6 lines, all of them trailing-space removals (`namespace dsp { ` -> `namespace dsp {` in
  `fft_complex.hpp`).  The fixed SSR FFT is functionally unchanged across the two releases, so
  replicating either source and validating against Vitis 2025.1 is sound.  Re-run that diff if
  the toolchain moves again — it is two commands and it is the cheapest risk retirement in
  this plan.
* **`CONVERGENT_RND` has no Waveflow equivalent.**  Believed inert in 2023.1 (nothing
  dispatches on it), so out of scope at the default.  The 2025.1 diff above confirms it is
  still inert there — the headers are functionally identical — so this holds for the toolchain
  actually being validated against.  If a later release ever implements it, it needs a new
  `QMode`, not a workaround.
* **Digit-reversal / output order.**  `SSR_FFT_NATURAL` vs `SSR_FFT_DIGIT_REVERSED_TRANSPOSED`
  is a permutation, not arithmetic, so it cannot cause a 1-LSB divergence — but it can cause a
  total mismatch that *looks* like one.  Pin the order explicitly in S2 before debugging any
  value.

## Sources

- [L1 SSR FFT user guide (2020.2)](https://xilinx.github.io/Vitis_Libraries/dsp/2020.2/user_guide/L1.html)
- Local checkouts, `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/` in each:
  - `/home/marco/AmirProjects/Vitis_Libraries_2025.1` — branch `2025.1` = `v2025.1_update2`
    (`b2c657d`), sparse to `dsp/L1`, **matches the installed toolchain**; prefer this one.
  - `/home/marco/AmirProjects/Vitis_Libraries` — `2023.1_motor_libs_update`, full checkout.
    Functionally identical for the fixed FFT (see version skew above).
- [Vitis_Libraries SSR FFT source](https://github.com/Xilinx/Vitis_Libraries/blob/master/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_traits.hpp)
