# `MatrixVectorMul_bitexact/` — what is in here

A bit-exact Python model of the **AMD Vitis BLAS L1 `gemv`** (`y = M x`): given the same input
bits, it produces the same output bits as the kernel, without running Vitis.

Verified against the shipped library on **2763 golden rows** — four element paths, six matrix
sizes, five stream widths, zero differences.

**Where to start:** [`VERIFY.md`](VERIFY.md) if you want to run something;
[`PLAN.md`](PLAN.md) if you want to know how it was built, what was measured, and what is still
open.

Sibling of [`../fft_bitexact/`](../fft_bitexact/), which applies the same method to a different
kernel.  The two probe genuinely different failure modes — see "Why this is not the FFT" below.

## Directory map

```
MatrixVectorMul_bitexact/
├── README.md          this file
├── VERIFY.md          how to check the claims yourself, at three levels of effort
├── PLAN.md            the working record: what was measured, what was wrong, what remains
│
├── wf_gemv/           THE MODEL -- the deliverable
├── golden/            reference outputs from the real Vitis library (checked in)
├── data/              the inputs those goldens were produced from
├── cpp/               the generators that produce golden/, + a one-line-patched header copy
├── tools/             input generators + one script that rebuilds every golden
└── tests/             the gates, run with plain pytest -- no Vitis needed
```

## Why this is not the FFT

Same method, different difficulty — which is the point of having both.

| | `fft_bitexact` | this |
|---|---|---|
| what hides the bits | fixed-point quantization rules | **summation order / associativity** |
| the naive model fails because | wrong rounding mode, wrong widths | right arithmetic, **wrong order** |
| the risky corner | `AP_RND` vs `AP_TRN`, quarter-wave tables | tree shape, `AdderDelay`, FMA contraction |
| what a wrong model costs | large, obvious errors | **1 ULP** — passes casual inspection |

Floating-point addition is not associative, so a dot product's bits are decided by the *shape* of
its reduction.  `numpy.dot` and a plain binary tree are both defensible code, and both disagree
with this kernel.  A model that is right for the FFT's reasons could still be wrong here.

## `wf_gemv/` — the model

The only directory whose contents matter to a *user* of this work.  Pure Python + numpy; the
fixed-point path imports `waveflow.utils.fixputils` and adds no quantization logic of its own.

| file | what it is |
|---|---|
| `gemv.py` | the float path (`dot`, `gemv`), the integer path (`dot_int`, `gemv_int`), and the `alpha`/`beta` overload (`scal`, `axpy`, `gemv_ab`) |
| `fixed.py` | the `ap_fixed` path — **two** models, because the library is wrong here: `gemv_fixed` is what the kernel computes, `gemv_fixed_as_shipped` is what it actually emits |

### The reduction the float path models

`DotHelper` dispatches on the element type: `float` and `double` go to `dot_tree`, everything
else to `dot_dsp`.  They are not variations on a theme — they are different reductions.

`dot_tree` is three orders in one:

```
preProcess    BinarySum over each beat of ParEntries   -> one value per beat   (a tree)
padding       pad the beat count up to a multiple of Delays
postProcess   per chunk of Delays beats: BinarySum     (a tree)
              then  finalSum += chunkResult            (SEQUENTIAL)
```

`Delays` is `AdderDelay<T>` — **4 for float, 8 for double, 1 otherwise** — the FP adder latency
the design pipelines around.  Not a user knob, and it changes the answer.

`dot_dsp` is one accumulator in index order, no tree at all.  So `parEntries` is part of the
numerical contract on the float path and irrelevant on the other — a model that carried one over
to the other would be wrong.  Both properties are pinned by tests.

## Two library defects, found by modelling it

Neither is a subtlety about the model; both are things the library does that its documentation
does not say.  Full detail in [`PLAN.md`](PLAN.md).

**`t_MacDataType` is exposed, documented, and uncompilable** (S3).  `gemv` declares its output
stream as `WideType<t_DataType,1>` while forwarding to a `DotHelper` parameterised on
`t_MacDataType`, so any differing MAC type fails to compile *inside* `gemv.hpp:47`.  Narrow
elements with a wide accumulator — the obvious way to avoid overflow — does not build.

**`gemv` returns wrong answers for every `ap_fixed` instantiation** (S4).  `dot_dsp` ends with
`p_res.write(l_res)`, which converts to the stream's `ap_uint<W>` **by value rather than by bit
pattern**.  For an integer that is the identity; for `ap_fixed` it truncates toward zero and the
consumer then unpacks the integer as a raw stored field:

```
ap_fixed<16,8>, true dot = 27.75  ->  stream carries 27  ->  reads back as 27/256 = 0.105469
```

Both are byte-identical in Vitis Libraries 2023.1 and 2025.1, so long-standing rather than
regressions.

## `golden/` — reference outputs, checked in

Produced by the **real Vitis library**, never by a reimplementation, and compared as raw bit
patterns rather than decimals.  Checked in so the tests need neither Vitis nor a compiler.

| files | path | rows |
|---|---|---|
| `gemv_f32_M*_N*_sweepP.txt` | float `dot_tree`, six sizes x five stream widths | 1320 |
| `gemv_ab_M*_N*.txt` | the `alpha`/`beta` overload, six (alpha, beta) pairs | 792 |
| `gemv_fixed_W*_*_shipped.txt` | `ap_fixed` **as the library ships** — i.e. wrong | 288 |
| `gemv_fixed_W*_*_patched.txt` | `ap_fixed` with the one-line defect corrected | 288 |
| `gemv_int_M3_N32.txt` | `dot_dsp`, int16 and int32 | 75 |

Float and `alpha`/`beta` goldens are IEEE-754 bit patterns; `ap_fixed` goldens are the raw stored
field in hex; the integer golden is decimal, where value and bit pattern coincide.

> Unlike `fft_bitexact/golden/`, these are `.txt`, so the repo's blanket `*.json` ignore rule
> does not swallow them.  That trap cost the FFT project a silent regression; worth knowing why
> it does not apply here.

## `cpp/` — golden generators

Each `dump_*.cpp` instantiates the shipped library and dumps what *it* produces.  All compile
natively under `g++` against `<hls_stream.h>`, so regenerating every golden takes ~19 seconds
rather than a Vitis run.

| file | dumps |
|---|---|
| `dump_gemv.cpp` | the float path, sweeping `logParEntries` 0..4 |
| `dump_gemv_int.cpp` | the `dot_dsp` path at int16 / int32 |
| `dump_gemv_fixed.cpp` | the `ap_fixed` path, sweeping (`QMode` x `OMode`) x stream width; the format width comes from the input file's header — built **twice** |
| `dump_gemv_ab.cpp` | the `alpha`/`beta` overload |

### `cpp/vendor_patched/dotHelper_patched.hpp`

A copy of one vendor header carrying **one changed line** — the S4 defect, fixed.  Included
*before* `<xf_blas.hpp>`, its own include guard suppresses the shipped copy, so nothing in the
vendor tree is edited and no include-path shadowing is needed.

`dump_gemv_fixed.cpp` is built both with and without it, giving the paired
`_shipped` / `_patched` goldens.  A test fails if a future release makes the two agree — the
signal to retire the as-shipped model rather than discover later that it describes a bug that no
longer exists.

This is the same technique as `fft_bitexact/cpp/vendor_debug/`, at one file instead of 45.

## `tools/`

| file | what it does |
|---|---|
| `gen_input.py` | float inputs — **searches** for vectors on which the candidate models provably disagree |
| `gen_input_fixed.py` | `ap_fixed` inputs — searches for data that overflows *some* rows and not others, so the `OMode` sweep carries information |
| `gen_input_ab.py` | `alpha`/`beta` inputs, with hand-chosen scalar pairs |
| `regen_golden.sh` | rebuilds every input and every golden, ~19s |

The generators **raise rather than settle for weaker data**.  A size that stops discriminating
announces itself instead of quietly blessing a wrong model — this caught a real error in the
suite's own discrimination rule (see `PLAN.md`, S5).

## `tests/`

Plain `pytest MatrixVectorMul_bitexact/tests/` — **45 tests, ~2.6 s, no Vitis and no compiler**,
because the goldens are checked in.

| file | tests | gate |
|---|---|---|
| `test_gemv.py` | 15 | the float path, six sizes x five widths |
| `test_gemv_ab.py` | 7 | the `alpha`/`beta` overload |
| `test_gemv_fixed.py` | 18 | `ap_fixed`, both builds, plus the defect's shape |
| `test_gemv_int.py` | 5 | `dot_dsp` at int16 / int32 |

Many of these assert that a **plausible wrong model fails**: `numpy.dot`, a plain binary tree,
quantizing once at the end instead of per element, ignoring `AP_RND`, and the fused reading of
`axpy`'s multiply-add.  A gate that cannot fail proves nothing, and every one of those is a
mistake that was actually available to make here.

## Conventions used throughout

* **Goldens come from the vendor's own code**, never a reimplementation — otherwise a test only
  proves two reimplementations agree with each other.
* **Comparisons are on raw stored bits.**  For float that means the IEEE-754 pattern; a `%.17g`
  round-trip hides exactly the 1-ULP differences this project exists to detect.
* **Measure, don't derive.**  Inherited from the FFT, where reading the templates produced a
  confident wrong answer three times.  It paid here too: the `ap_fixed` defect was found by an
  instantiation returning zeros, and the chunk-count rule in S5 by a sweep, not by reasoning.
* **Every gate must be able to fail**, and the input generators enforce it.
