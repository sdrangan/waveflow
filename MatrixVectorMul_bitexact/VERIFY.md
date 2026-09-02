# Verifying this yourself

Three levels, in increasing order of what they need and what they prove.  Level 1 needs nothing
but Python; level 3 is the one that checks something the tests structurally cannot.

Run everything from the repository root.

| | needs | takes | proves |
|---|---|---|---|
| **1. Run the gates** | Python + numpy | ~2.6 s | the model matches 2763 checked-in golden rows |
| **2. Rebuild the goldens** | + `g++`, Vitis headers, BLAS source | ~19 s | those goldens really do come from the vendor's code |
| **3. Vary the compiler** | same as 2 | ~10 s | the FMA caveat is real, and the golden's build flags matter |

## Level 1 — run the gates

```bash
source env/bin/activate
pytest MatrixVectorMul_bitexact/tests/ -q
```

Expected: **45 passed** in about 2.6 seconds.  No Vitis, no compiler, no network — the goldens
are checked in.

That covers four element paths and 2763 rows:

| path | golden rows | what it pins |
|---|---|---|
| float `dot_tree` | 1320 | the three-order reduction, six sizes x five stream widths |
| `alpha`/`beta` overload | 792 | `scal` then `axpy`, six scalar pairs |
| `ap_fixed` | 576 | per-element Q/O narrowing — half as shipped, half corrected |
| integer `dot_dsp` | 75 | single-accumulator wrapping at int16 / int32 |

## Level 2 — rebuild every golden from the shipped library

This is the check that matters most, because it is what makes level 1 mean anything: it
re-derives every reference output by compiling and running **AMD's own code**.

```bash
source env/bin/activate
cd MatrixVectorMul_bitexact
bash tools/regen_golden.sh
git status --short data/ golden/
```

Expected: `git status` reports **nothing**.  Every input and every golden is regenerated
byte-for-byte, including the random-search inputs (the generators are seeded).

Defaults, overridable by environment variable:

| variable | default |
|---|---|
| `VITIS_INC` | `/tools/Xilinx/2025.1/Vitis/include` |
| `BLAS_INC` | `/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw` |
| `PY` | `../env/bin/python` |
| `FPFLAGS` | `-ffp-contract=off` — see level 3, this one is not cosmetic |

Only the headers are needed, not a Vitis *run*: in C-simulation `hls::stream` is an ordinary
queue and `ap_fixed` is ordinary integer arithmetic, so `g++` compiles the library directly.

Then re-run level 1 against the regenerated goldens.  If the tests still pass, the model agrees
with a fresh build of the vendor library rather than with a stale file.

## Level 3 — vary the compiler, and watch the answer change

⚠️ **This is the one caveat on the phrase "bit-exact" in this project, and it is checkable in
ten seconds.**

`axpy` (`axpy.hpp:71`) computes `p_alpha * l_realX + l_realY` as a **single expression**.  That
is a fused-multiply-add candidate: an FMA keeps the product's full precision and rounds once,
where a separate multiply and add round twice.  The two give different bits.

```bash
cd MatrixVectorMul_bitexact
INC="-I/tools/Xilinx/2025.1/Vitis/include \
     -I/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw \
     -I/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw/xf_blas"

g++ -std=c++14 -O3 -march=native $INC -o /tmp/ab_fma cpp/dump_gemv_ab.cpp
/tmp/ab_fma data/input_ab_M4_N64.txt /tmp/ab_fma.txt

diff <(grep -v '^#' golden/gemv_ab_M4_N64.txt) \
     <(grep -v '^#' /tmp/ab_fma.txt) | grep -c '^<'
```

Expected: **25**.  Twenty-five of 576 rows change, from nothing but a compiler flag.

The full picture, all four builds of the same source:

| build | vs the checked-in golden |
|---|---|
| `-O0` | identical |
| `-O2` | identical |
| `-O2 -ffp-contract=off` | identical |
| `-O3 -march=native` | **25 rows differ** |
| `-O2 -mfma -ffp-contract=fast` | **25 rows differ** |

`-O0` suppresses contraction on this host only because the default `-march` has no FMA.  On a
host whose baseline includes it, the golden would silently change — which is why
`regen_golden.sh` passes `-ffp-contract=off` explicitly rather than relying on the optimization
level.  `test_an_fma_model_gives_different_bits` fails if that distinction ever stops mattering,
so the pin cannot quietly become unfalsifiable.

**The float `dot_tree` path is not exposed to this**, and that was measured, not assumed: it is
byte-identical across all four builds, because it has no multiply-add in one expression.  The
exposure is specific to `axpy`, and therefore specific to the `alpha`/`beta` overload.

## What is *not* verified here — and how this differs from `fft_bitexact`

The FFT project has two runnable Vitis packages ([`verifyFFT16/`](../fft_bitexact/verifyFFT16/)
and [`verifyFFT1024/`](../fft_bitexact/verifyFFT1024/)) that take the design through
C-simulation → C-synthesis → C/RTL co-simulation and compare all three.  **This project has no
equivalent.** Every golden here comes from native C-simulation.

That gap has one concrete consequence, and it is the open question level 3 raises:

> **Does Vitis HLS emit a fused or an unfused operator for `axpy`'s `alpha*x + y`?**

Only synthesis can answer it, and the answer decides which of the two readings the *hardware*
matches.  For the float `dot_tree` path the question does not arise, and for the integer and
`ap_fixed` paths the arithmetic is integral, so the exposure is confined to the `alpha`/`beta`
overload.

Two smaller things are also unverified against RTL: that C-simulation and co-simulation agree at
all here (they do for the FFT), and that `WideType`'s packing behaves the same under synthesis —
it uses `sizeof(T)*8` as the slot width, which for `ap_fixed<24,12>` is 32 bits in C-simulation
and would be 24 under `__SYNTHESIS__`.  That affects the stream layout, not the arithmetic the
model reproduces, but it has not been measured.

Building a `verifyGEMV/` package on the FFT's pattern is the natural next step and would settle
all three.

## If something fails

| symptom | likely cause |
|---|---|
| `hls_stream.h not under VITIS_INC` | Vitis not installed at the default path — set `VITIS_INC` |
| `xf_blas.hpp not under BLAS_INC` | Vitis Libraries not cloned, or `blas` not in the sparse checkout — set `BLAS_INC` |
| level 2 leaves `git status` dirty | a different library version; check the `PLAN.md` note that `gemv.hpp` and `dotHelper.hpp` are byte-identical between 2023.1 and 2025.1 before assuming a real change |
| `test_the_shipped_kernel_is_wrong` fails | AMD fixed the `ap_fixed` defect — good news, and the signal to retire `gemv_fixed_as_shipped` |
| `test_wider_mac_type_is_uncompilable_in_the_library` fails | AMD fixed `t_MacDataType` — the integer model's single-`width` assumption needs revisiting |
| a generator raises "no discriminating vector" | the test data has stopped separating the model from a plausible wrong one; **enlarge the case, do not lower the bar** |
