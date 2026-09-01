# Bit-exact Vitis BLAS matrix-vector multiply — the plan

**Status:** PLAN (2026-09-01).  **S1 premise confirmed** — see "First measurement" below.  Sibling of [`../fft_bitexact/`](../fft_bitexact/), which is
finished for `L = 4^S`; this applies the same method to a different kernel.  Everything below was
checked against the shipped source and the installed toolchain, not assumed.

**Goal:** a Python model of the Vitis BLAS `gemv` that reproduces the hardware's output **bit for
bit**, so a fixed-point or float matrix-vector datapath can be designed at Python speed and still
predict what the FPGA emits.

---

## ⚠️ Read this first: "systolic array" does not describe any GEMV in this library

The request was for *matrix-vector multiplication based on systolic arrays*.  That combination
does not exist in Vitis BLAS.  Searching every header and source for `systolic` (case-insensitive)
returns **GEMM only** — `gemmSystolicArray.hpp`, `gemmKernel.hpp`, `gemmMuls.hpp`,
`gemmMulsSink.hpp`.  There is no systolic GEMV.

What does exist:

| candidate | what it is | systolic? | true GEMV? |
|---|---|---|---|
| `blas/L1/include/hw/xf_blas/gemv.hpp` | `y = M x`, streaming, parallel reduction | no — a reduction tree | **yes** |
| `blas/L2/.../gemmSystolicArray.hpp` | a real systolic array | **yes** | no — GEMM |
| `blas/L2/.../gemvKernel.hpp` (`krnl_gemv`) | deployable multi-channel kernel | no | yes |

**This plan targets L1 `gemv`** — the operation asked for, in the layer that matches what worked
for the FFT (self-contained, C-simulable, synthesizable on its own).  `krnl_gemv` is rejected for
now because it is a platform kernel with `BLAS_numChannels` DDR interfaces; it needs XRT and a
shell rather than plain `csim`/`cosim`, which would put the toolchain in the way of the modelling.

If the systolic array is what actually matters, say so — the honest way to get it is
**S6** below (GEMM with `N = 1` *is* a matrix-vector product), and it is a different project shape,
not a tweak.

---

## What makes this different from the FFT — and why it is still interesting

The FFT's difficulty was **hidden fixed-point quantization**: quarter-wave tables, per-stage
requantization, narrow-before-rotate.  `gemv` has almost none of that.  Its difficulty is
somewhere else, and it is real.

`DotHelper` (`helpers/funcs/dotHelper.hpp:108-152`) dispatches on the data type:

    generic t_DataType   ->  dot_dsp    (DSP-oriented accumulation)
    float                ->  dot_tree   (binary reduction TREE)
    double               ->  dot_tree

**Floating-point addition is not associative.**  A binary tree over 2^k products gives different
bits from a sequential accumulation over the same products — so `numpy.dot`, Python's `sum`, and
this kernel are three different answers, and only one of them is the hardware's.  Reproducing the
bits means reproducing **the reduction order**, exactly.

So the two projects probe different failure modes with the same method:

| | `fft_bitexact` | this |
|---|---|---|
| what hides the bits | quantization rules | **summation order / associativity** |
| naive model fails because | wrong rounding mode, wrong widths | right arithmetic, wrong *order* |
| the risky corner | `AP_RND` vs `AP_TRN`, table reads | tree shape, accumulator type, `t_MacDataType` |

That is worth having: a model that is right for the FFT's reasons could still be wrong here.

---

## Method — inherited wholesale from `fft_bitexact`

These are not restated per stage; they are the standing rules, and each one was earned there:

1. **Goldens come from the vendor's own code**, never a reimplementation.  Otherwise a test only
   proves two reimplementations agree.
2. **Compare raw stored bits, not decimals.**  For float that means the IEEE-754 bit pattern —
   a `%.17g` round-trip is not good enough and hides exactly the 1-ULP differences at issue.
3. **Measure, don't derive.**  Three times in the FFT work, reading the templates produced a
   confident wrong answer that instrumentation settled in minutes.  Instrument early here.
4. **Every gate must be able to fail.**  Each stage carries a test asserting the *plausible wrong
   model* differs — for this kernel, sequential-vs-tree summation is the obvious one.
5. **Goldens are checked in** so the tests need neither Vitis nor a compiler.  (Note the repo's
   root `.gitignore` has a blanket `*.json`; `fft_bitexact/golden/` had to be re-included
   explicitly.  This project will need the same and it is easy to miss.)

## Environment — already checked

| | |
|---|---|
| BLAS source | `/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas` (added to the sparse checkout) |
| Toolchain | Vitis 2025.1, as for the FFT |
| Version skew | **not a risk**: `blas/L1/include/hw/xf_blas/gemv.hpp` is byte-identical between 2023.1 and 2025.1; only `gemm.hpp` differs (12 lines) |
| Native build | expected to work as it did for the FFT — `hls::stream` in csim mode is an ordinary queue |

## The target

`xf::blas::gemv<t_DataType, t_LogParEntries, t_IndexType, t_MacDataType>(m, n, M, x, y)`
computing `y = M x`, with `M` and `x` arriving as `WideType` streams of `2^t_LogParEntries`
entries per beat.  A second overload adds `alpha`/`beta`.

v1 configuration, to be confirmed by measurement rather than assumed:

* `t_DataType = float` — this is the path with the reduction tree, i.e. the interesting one, and
  it is what the shipped tests exercise
* small `m`, `n` (e.g. `m = 4`, `n = 16`) — diffable by eye when a row disagrees
* `t_LogParEntries` = 2 to start, then varied, since it *is* the tree depth

---

## First measurement — the premise holds, and `numpy.dot` is wrong

Before writing any model, two things were checked, because the whole project rests on them.

**1. BLAS builds and runs natively**, as the FFT library did — `g++` against `<hls_stream.h>` and
`xf_blas.hpp`, no Vitis run.  So goldens will cost milliseconds here too.

One trap found immediately: `gemv` consumes the vector stream **once per row**, so a hand-fed `x`
starves and the program hangs rather than erroring.  The library's own testbench
(`L1/tests/hw/gemv/uut_top.cpp`) uses `vec2GemStream`, which repeats `x` for each row.  Use the
shipped data movers — `gem2Stream`, `vec2GemStream`, `writeStream2Vec` — not hand-rolled feeds.

**2. Summation order really does decide the bits.**  `M = 4`, `N = 16`, `t_LogParEntries = 2`,
`float`, comparing IEEE-754 bit patterns against the real kernel:

| model | result |
|---|---|
| `numpy.dot` on `float32` | **differs** — 1 ULP on row 2 |
| sequential accumulate | **differs** — 1 ULP on rows 1, 2, 3 |
| binary reduction tree | **matches** all four rows |
| per-beat tree, then accumulate beats | matches — but `N/P = 4` here, so not yet discriminating |

So the obvious model is wrong, by one unit in the last place, on data with no extreme values.
That is the project's justification, established before any code was written — and it is the same
shape of finding as the FFT's `AP_RND` twiddles: the natural guess is defensible, common, and
wrong.

The last two rows agree with each other at this size.  Distinguishing them needs `N/P > 4`, which
is the first thing S1 proper must do — otherwise the gate cannot tell a full tree from a
per-beat tree, and the model would be unpinned in exactly the dimension that matters.

## Stages

**S1 — the reduction tree.**  Model `dot_tree` alone for one row: fixed products in, one float
out, bit-exact.  Isolates the associativity question with nothing else in frame, exactly as the
FFT's S1 isolated the twiddle table.  Deliverable: a C++ dumper, a checked-in golden, and a test
asserting that **sequential summation gives a different answer** — if it does not, the case is too
small to be discriminating and must be enlarged.

**S2 — full `gemv`.**  All rows, the wide-stream layout, `t_LogParEntries` varied.  Golden from
the real `xf::blas::gemv`.

**S3 — `t_MacDataType` and the non-float path.**  The generic specialization uses `dot_dsp`, not
the tree, and `t_MacDataType` lets the accumulator differ from the element type.  Both change the
bits.  Measure before modelling.

**S4 — fixed point.**  `gemv` is templated, so `ap_fixed` can be instantiated even though the
shipped tests do not.  This is where `waveflow`'s `FixedField` earns its place and where the work
connects back to the library, as `cquantize` did for the FFT.

**S5 — `alpha`/`beta` overload**, and `m`/`n` sweeps.

**S6 — the systolic GEMM, if wanted.**  `gemmSystolicArray` with `N = 1` computes a
matrix-vector product on a genuine systolic array.  Different kernel, different data movement,
its own goldens.  Listed because it is what "systolic" would actually mean here — not because it
follows from S1-S5.

## Open questions

* **Which is actually wanted, L1 `gemv` or the systolic GEMM?**  The plan proceeds with `gemv`;
  S6 is the other reading and would be a fresh start rather than an extension.
* **Does float `dot_tree` give a stable answer under `-O` and across csim/cosim?**  It must, or
  bit-exactness is not a meaningful target.  S1 answers this before anything is built on it.
* **Is `t_LogParEntries` part of the contract or an implementation detail?**  If the bits change
  with it, a model must take it as a parameter, and any design that retunes it changes its output.
