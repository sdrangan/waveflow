# Bit-exact Vitis BLAS matrix-vector multiply — the plan

**Status:** **S1-S6 DONE** (2026-09-02) — bit-exact on **2763 golden rows**: float 1320, `alpha`/`beta` 792, `ap_fixed` 576 (half as the library ships, half with its defect corrected), integer 75 — plus 121 rows against **synthesized RTL** in `verifyGEMV/`.  S3, S4 and S5 each found something in the library; **the S4 one makes `gemv` return wrong answers for every `ap_fixed` instantiation**, and the S5 one makes "bit-exact" conditional on the compiler not fusing a multiply-add.  Premise confirmed — see "First measurement" below.  Sibling of [`../fft_bitexact/`](../fft_bitexact/), which is
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

**Settled (2026-09-02): the systolic reading is dropped and this project is the L1 `gemv`.**  The
section stays because it records why the request as phrased could not be met literally — nothing
in Vitis BLAS is both systolic and a GEMV — not because the option is still open.

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

**S1 — the reduction tree.**  ✅ **DONE — bit-exact, 32/32 rows over 8 cases.**

The structure is not a tree.  `sum()` (`helpers/funcs/sum.hpp:104-118`) is three stages::

    preProcess   BinarySum over each beat of ParEntries    -> one value per beat
    padding      pad the beat count to a multiple of Delays
    postProcess  per chunk of Delays beats: BinarySum (tree), then finalSum += (SEQUENTIAL)

So it trees *within* a beat, trees *within* a chunk, and accumulates *across* chunks
sequentially — three orders in one reduction.  `Delays` is `AdderDelay<T>` — **4 for float, 8 for
double, 1 otherwise** (`helpers/utils/utils.hpp:91-109`) — the FP adder latency the design
pipelines around.  Not a user knob, and it changes the answer.

Measured against the real kernel, 32 rows:

| model | rows wrong |
|---|---|
| **library structure** | **0** |
| naive full binary tree | 7 |
| `numpy.dot` | 16 |

**Two traps this stage exposed, both about test data rather than code.**  A first attempt at
`M=4, N=16` matched a full tree, a per-beat tree *and* the hardware simultaneously — the case was
too small to discriminate.  And `numpy.dot` matched at `N=64` with simple data while differing at
`N=16`: it is *unreliably* right, which is worse than reliably wrong, because a small suite
blesses it.  `tools/gen_input.py` therefore searches for vectors on which the candidate models
provably disagree, and the tests assert that both wrong models still fail — a gate that stops
discriminating announces itself.

**Original S1 text, for the record:**  Model `dot_tree` alone for one row: fixed products in, one float
out, bit-exact.  Isolates the associativity question with nothing else in frame, exactly as the
FFT's S1 isolated the twiddle table.  Deliverable: a C++ dumper, a checked-in golden, and a test
asserting that **sequential summation gives a different answer** — if it does not, the case is too
small to be discriminating and must be enlarged.

**S2 — full `gemv`.**  ✅ **DONE — bit-exact, 460/460 rows.**

Six sizes after the S5 sweep, five stream widths and eight cases each — **1320 rows, 0 differ**:

| size | rows | widths where a full tree and the library differ at all |
|---|---|---|
| `M=1, N=16` | 40 | P=1 |
| `M=2, N=48` | 80 | P=1, 2 |
| `M=16, N=32` | 640 | P=1, 2 |
| `M=4, N=64` | 160 | P=1, 2, 4 |
| `M=3, N=176` | 120 | P=1, 2, 4, 8 |
| `M=7, N=128` | 280 | P=1, 2, 4, 8 |

The last three sizes and the width column arrived with S5; `N=48` and `N=176` are deliberately not
powers of two, which the library supports and which a full-tree comparison model has to be
zero-padded to handle at all.

`logParEntries` is swept 0..4 in one golden, because S1 showed the stream width changes the bits.
It does: on a discriminating case the five widths give four distinct answers, so the sweep is not
padding.

**Some size/width combinations cannot discriminate at all, and the exact condition is now
measured.**  A zero-padded full binary tree and the library's reduction are the *same* reduction
whenever the chunk count `ceil((N/P) / Delays)` is **3 or fewer** — 0 differences in 400 random
trials for every such combination tested, and frequent differences for every combination at 4 or
more:

    k = 3   (c0 + c1) + c2          == ((0 + c0) + c1) + c2      the padded slot contributes 0
    k = 4   (c0 + c1) + (c2 + c3)   != ((c0 + c1) + c2) + c3      the first real difference

S2 originally recorded this as *"one chunk"*, generalising from `M=1, N=16` at `P=4`.  **That was
too narrow**, and S5 caught it: adding `(2, 48)`, where `P=4` gives 12 beats but only 3 chunks,
made the generator's guard — `N // P > DELAYS` — raise a false alarm on data that no search could
have found.  The guard now uses the measured condition, the search runs at every swept width
rather than only `P=4`, and a test asserts both halves of the rule against the real goldens.

The first version of this was found the hard way too: the generator searched for a discriminating
vector at `M=1, N=16` and looped forever.  It is bounded, and it raises if a size that *should*
discriminate does not.

**S3 — the non-float path.**  ✅ **DONE — bit-exact, 75/75 rows** (int32 and int16, three stream
widths, 5 cases).

`dot_dsp` (`helpers/funcs/dotHelper.hpp:75-100`) is nothing like the float path::

    t_MacDataType l_res = 0;
    for each beat:  for j in 0..parEntries-1:  l_res += l_x[j] * l_y[j];

One accumulator, index order, no tree.  Two consequences, both measured and both pinned by tests:

* **`parEntries` does not change the result here** — 0 of 30 rows vary across the swept widths,
  where on the float path it sets the tree shape and *is* part of the numerical contract.  A model
  that carried the float handling over would be wrong.
* **The accumulator wraps.**  34 of 75 rows differ from an unwrapped (arbitrary-precision) sum, so
  the goldens genuinely exercise it rather than passing for a model with no wrapping at all.

### ⚠️ `t_MacDataType` is exposed, documented — and uncompilable

The half of S3 about widening the accumulator **cannot be done**, and not for modelling reasons.
`gemv` declares its output stream as `WideType<t_DataType, 1>` and then forwards it to
`DotHelper<..., t_MacDataType>::dot`, which expects `WideType<t_MacDataType, 1>`::

    gemv.hpp:47: error: cannot convert 'hls::stream<ap_uint<16> >' to 'hls::stream<ap_uint<32> >&'

So any `t_MacDataType != t_DataType` fails to compile *inside the library*, on **both** the 5-arg
and 8-arg overloads.  `dot()` escapes only by never passing the parameter.  Identical in 2023.1
and 2025.1, so long-standing rather than a regression.

This is why `gemv_int` takes one `width` rather than separate element and accumulator widths: the
narrow-elements-wide-accumulator configuration a user would reach for to avoid overflow **does not
build**.  A test asserts the header still has that shape, so if AMD fixes it the model's
single-width assumption is flagged instead of quietly becoming wrong.

**S4 — fixed point.**  ✅ **DONE — bit-exact, 288/288 rows** (`ap_fixed<16,8>` and
`ap_fixed<24,12>`, four (Q, O) combinations, three stream widths, two builds).

`ap_fixed` is not `float`, so it takes `dot_dsp` — the same single-accumulator path as S3.  With
`t_MacDataType` pinned to the element type (S3), the accumulator **is** the element format, so
every `l_res += l_x[j] * l_y[j]` narrows back to `<W,I>` and applies the format's Q and O modes::

    product          exact -- ap_fixed<W,I> * ap_fixed<W,I> is ap_fixed<2W,2I>
    l_res + product  exact -- the operator's return type is wide enough
    assignment       LOSSY -- narrowing applies Q, then O

The knobs therefore move from summation order (the float path) to **quantization and overflow,
applied once per element rather than once at the end**.  That is the S4 gate: the model that
accumulates exactly and rounds once — what anyone would write from the docs, and what a DSP MAC
with a wide accumulator would do — is wrong on **132/144** rows at `W=16` and **114/144** at
`W=24`.  Ignoring `AP_RND` misses every `AP_RND` row (72/144).  `parEntries` again does not
change the result, and again that is measured, not carried over.

This is where `waveflow`'s numeric core earns its place: `wf_gemv/fixed.py` is `fixputils.mult`,
`fixputils.add` and `fixputils.quantize` in a three-line loop, with the `Q`/`O` semantics already
validated by the FFT work.  Its one limit is `W <= 31` — the exact `acc + product` intermediate
needs `2W+1` bits and `fixputils` is int64-backed — and `fixed_format` raises at construction
rather than wrapping silently.

### ⚠️ `gemv` returns wrong answers for every `ap_fixed` instantiation

Not a modelling subtlety — a one-line defect that corrupts the result.  `dot_dsp` ends with
(`helpers/funcs/dotHelper.hpp:98`)::

    p_res.write(l_res);        // l_res is t_MacDataType; the stream carries ap_uint<W>

That conversion is **numeric, not a bit repack**: it truncates toward zero to the integer part,
which the consumer then unpacks as a raw stored field.  Measured, `ap_fixed<16,8>`::

    true dot = 27.75  ->  stream carries 27  ->  reads back as 27/256 = 0.105469

Every fractional bit is gone and the magnitude is off by `2**F`.  The float path does not have
this: `postProcess` (`sum.hpp:79`) writes a `WideType`, whose `operator t_TypeInt` packs by
`reinterpret_cast`.  Two sibling reductions, two conventions.  For an integer element type the
numeric conversion is the identity, which is why S3 never saw it.

`dotHelper.hpp` is **byte-identical between 2023.1 and 2025.1**, so this is long-standing.  It
also survives to RTL: the conversion is C++ semantics, not a csim artifact.

Both behaviours are modelled and both goldens come from the library's own code:

| golden | built from | model |
|---|---|---|
| `*_shipped.txt` | the untouched library | `gemv_fixed_as_shipped` |
| `*_patched.txt` | `cpp/vendor_patched/dotHelper_patched.hpp` — a copy of that one header with that one line changed, put ahead of the shipped copy by its own include guard | `gemv_fixed` |

Nothing in the vendor tree is edited.  `test_the_shipped_kernel_is_wrong` fails if a future
release makes the two goldens agree — the signal to retire `as_shipped` rather than to discover
later that the model is describing a bug that no longer exists.

**Not covered:** `ap_ufixed`; `W > 31`; and the two-defect interaction is unexplored, since with
`t_MacDataType` uncompilable there is no configuration in which both could be exercised at once.

**S6 — Vitis verification.**  ✅ **DONE — 9/9 comparisons, 121 rows** ([`verifyGEMV/`](verifyGEMV/)).

C-simulation, C-synthesis and C/RTL co-simulation of three DUTs, each compared against the model
and against each other.  It answered both questions the native builds could not reach: **HLS does
not fuse `axpy`'s multiply-add**, and **the `ap_fixed` defect is in the synthesized hardware**,
not a C-simulation artifact.  See that folder's `README.md` and `ARCHITECTURE.md`.

One thing worth recording for anyone building a similar package: with `hls::stream` as top-level
arguments, `csim` and `csynth` both pass and co-simulation then aborts with *"an hls::stream is
read while empty"* while instrumenting the testbench.  Taking arrays and doing the stream
plumbing inside the top -- the shape the library's own `L1/tests/hw/gemv/uut_top.cpp` uses --
cosims cleanly.

**S5 — `alpha`/`beta` overload, and `m`/`n` sweeps.**  ✅ **DONE — bit-exact, 792/792 rows**
(6 (alpha, beta) pairs x 3 stream widths x 4 cases, at `M=4, N=64` and `M=7, N=128`), plus three
new sizes on the float path taking it from 460 to 1320 rows.

The 8-arg overload (`gemv.hpp:66-85`) computes `yr = alpha * (M x) + beta * y` and introduces no
new kernel — it is a composition of three shipped ones::

    gemv(...)  ->  l_x        the 5-arg overload, i.e. all of S1-S2
    scal(...)  ->  l_y        l_y[j] = beta * y[j]                 (scal.hpp:66)
    axpy(...)  ->  yr         yr[j]  = alpha * l_x[j] + l_y[j]     (axpy.hpp:71)

Two things decide the bits, and both are gated by tests that fail if the data stops separating
them.  `beta * y` is **rounded to float32 in `scal` before `axpy` adds it**, so the natural model —
evaluate `alpha*dot + beta*y` in one wider expression and round once — is wrong.  And the
composition is checked at the ends: `(alpha, beta) = (1, 0)` must reproduce the 5-arg overload
exactly, `(0, 1)` must return `y` untouched.

Unlike the 5-arg overload this one takes no `t_MacDataType`.  It also declares
`const unsigned int l_numIter` and never uses it — harmless, but a second piece of dead code in
the same file as S3's dead template parameter.

### ⚠️ "Bit-exact" is conditional on the compiler not fusing the multiply-add

`axpy` writes `p_alpha * l_realX + l_realY` as a single expression.  That is a fused-multiply-add
candidate: an FMA keeps the product's full precision and rounds **once**, where a separate
multiply and add round **twice**.  Building the same dumper both ways:

| build | vs `-O0` |
|---|---|
| `-O2` | identical |
| `-O2 -ffp-contract=off` | identical |
| `-O3 -march=native` | **25 of 576 rows differ** |
| `-O2 -mfma -ffp-contract=fast` | **25 of 576 rows differ** |

`-O0` suppresses it on this host only because the default `-march` has no FMA; on a host whose
baseline includes it the golden would silently change.  So `tools/regen_golden.sh` now passes
`-ffp-contract=off` explicitly, and a test asserts the fused reading still disagrees — if it ever
stops, the pin has become unfalsifiable and should be removed rather than trusted.

Verified at the same time, answering a standing open question: **the float `dot_tree` path is
stable** across `-O0`, `-O2`, `-O3 -march=native` and `-ffp-contract=off`, byte-for-byte.  It has
no multiply-add in one expression, so there is nothing to contract — the exposure is specific to
`axpy`, and therefore specific to this overload.

**Not measured:** what Vitis HLS itself does.  Whether synthesis emits a fused or an unfused
operator for that line is a question about the RTL, not about csim, and it is the one thing that
would decide which of the two goldens the hardware matches.

**Systolic GEMM — dropped.**  Earlier drafts listed a `gemmSystolicArray` stage as the other
reading of "systolic".  It is **out of scope** by decision (2026-09-02): this project is the L1
`gemv`, and that is a different kernel with different data movement and its own goldens.

## Open questions

* ~~**Which is actually wanted, L1 `gemv` or the systolic GEMM?**~~  **Settled (2026-09-01):
  L1 `gemv` is the target**, and as of 2026-09-02 the systolic GEMM is dropped outright rather
  than merely unplanned.
* ~~**Does float `dot_tree` give a stable answer under `-O`?**~~  **Settled in S5 by measurement:
  yes** — byte-identical across `-O0`, `-O2`, `-O3 -march=native` and `-ffp-contract=off`.  But
  the `alpha`/`beta` overload is **not**, because `axpy` has a contractable multiply-add; see S5.
* ~~**Does Vitis HLS emit a fused or an unfused operator for `axpy`'s `alpha*x + y`?**~~
  **Settled (2026-09-02): it does NOT fuse.**  `verifyGEMV/` synthesizes the overload and runs it
  in co-simulation; the RTL matches the unfused model on 96/96 rows and the fused one on 92/96,
  and the data separates the two on 4 rows, so the check is not vacuous.  Corroborated
  structurally: the synthesis report instantiates `fmul_32ns_32ns_32_4_max_dsp_1` and
  `fadd_32ns_32ns_32_5_full_dsp_1` as separate cores.  The `-ffp-contract=off` pin therefore
  matches the hardware rather than being a defensible guess.
* ~~**Is the `ap_fixed` defect a C-simulation artifact?**~~  **No — it is in the RTL.**
  `gemv_fixed_top` co-simulates to exactly what `gemv_fixed_as_shipped` predicts, 9/9 rows.
* ~~**Is `t_LogParEntries` part of the contract or an implementation detail?**~~  **Settled by
  measurement: it depends on the path.**  On `dot_tree` (float, double) it sets the tree shape and
  four of five swept widths give distinct answers, so a model must take it as a parameter and any
  design that retunes it changes its output.  On `dot_dsp` (integer, `ap_fixed`) it changes
  nothing — the accumulation is in index order at any width.  Tests pin both.
* **Should the `ap_fixed` defect be reported upstream?**  It is a one-line fix and it silently
  corrupts every fixed-point `gemv`/`dot`.  Out of scope here, but the patched header and the
  paired goldens are exactly the reproducer a report would need.
