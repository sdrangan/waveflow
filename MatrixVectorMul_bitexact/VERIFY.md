# Verifying this yourself

Three levels, in increasing order of what they need and what they prove.  Level 1 needs nothing
but Python; level 3 is the one that checks something the tests structurally cannot.

Run everything from the repository root.

| | needs | takes | proves |
|---|---|---|---|
| **1. Run the gates** | Python + numpy | ~3 s | the model matches 2877 checked-in golden rows |
| **2. Rebuild the goldens** | + `g++`, Vitis headers, BLAS source | ~19 s | those goldens really do come from the vendor's code |
| **3. Vary the compiler** | same as 2 | ~10 s | the FMA caveat is real, and the golden's build flags matter |
| **4. Run it through Vitis** | + Vitis HLS and Vivado `xsim` | ~4 min | the model matches **synthesized RTL**, not just C-simulation |

## Level 1 — run the gates

```bash
source env/bin/activate
pytest MatrixVectorMul_bitexact/tests/ -q
```

Expected: **52 passed** in about 3 seconds.  No Vitis, no compiler, no network — the goldens
are checked in.

That covers five element paths and 2877 rows:

| path | golden rows | what it pins |
|---|---|---|
| float `dot_tree` | 1320 | the three-order reduction, six sizes x five stream widths |
| `alpha`/`beta` overload | 792 | `scal` then `axpy`, six scalar pairs |
| `ap_fixed` | 576 | per-element Q/O narrowing — half as shipped, half corrected |
| integer `dot_dsp` | 135 | wrapping, **signed and unsigned**, widths 8/16/32/64 |
| double `dot_tree` | 54 | the same reduction grouped by 8 rather than 4 |

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

## Level 4 — run it through Vitis

[`verifyGEMV/`](verifyGEMV/) takes the real kernel through **C-simulation → C-synthesis → C/RTL
co-simulation** and compares every result against the Python model.  Co-simulation *is* the RTL
simulation: Vitis has no separate RTL-sim step for an `ap_ctrl_hs` kernel, it drives the
synthesized RTL with the same testbench in `xsim`.

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /tools/Xilinx/2025.1/Vivado/settings64.sh
source env/bin/activate
export WF_BLAS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw

cd MatrixVectorMul_bitexact/verifyGEMV
vitis-run --mode hls --tcl run.tcl     # ~4 min
python verify.py
```

Expected: **9/9 comparisons pass** — three DUTs x (C-sim vs model, co-sim vs model, C-sim vs
co-sim), 121 rows.

| DUT | question only synthesis could answer | verdict |
|---|---|---|
| `gemv_f32_top` | does the `dot_tree` reduction survive to RTL? | ✅ yes, 16/16 rows |
| `gemv_ab_top` | **does HLS fuse `axpy`'s `alpha*x + y`?** | ✅ **no** |
| `gemv_fixed_top` | is the `ap_fixed` defect real hardware or a csim artifact? | ⚠️ **real hardware** |

### The FMA question is settled

Level 3 shows the *C compiler's* choice changes 25 of 576 rows.  Level 4 shows what the
**synthesis tool** chose, on two independent lines of evidence:

```
rows on which the two readings differ at all : 4/96
RTL matches the UNFUSED (separate mul + add) : 96/96
RTL matches the FUSED (single rounding)      : 92/96
=> Vitis HLS does NOT fuse it.
```

and the synthesis report instantiates two separate cores — `fmul_32ns_32ns_32_4_max_dsp_1` and
`fadd_32ns_32ns_32_5_full_dsp_1` — rather than a fused MAC.

The first line is the important one: `verify.py` reports `INCONCLUSIVE` rather than claiming a
result if no row separates the two readings.

So the `-ffp-contract=off` pin in `tools/regen_golden.sh` **matches the hardware**, rather than
merely being a defensible choice among two.

## What is still not verified

* **Only the swept configuration is synthesized.**  `verifyGEMV/` covers `M=4, N=64, P=4` for
  float and `M=3, N=32, P=4` for `ap_fixed`.  The native goldens cover six sizes and five stream
  widths; RTL covers one of each.  Nothing suggests the others differ — the reduction structure
  is the same code — but it has not been measured.
* **`double` is modelled but not synthesized.**  Its golden comes from C-simulation only; no
  DUT in `verifyGEMV/` uses it.
* **`ap_ufixed` and `ap_fixed` wider than 31 bits** are unmodelled.
* **`WideType`'s packing under synthesis.**  It uses `sizeof(T)*8` as the slot width, which for
  `ap_fixed<24,12>` is 32 bits in C-simulation and would be 24 under `__SYNTHESIS__`.  The
  synthesized `ap_fixed` DUT uses `<16,8>`, where `sizeof` is exactly 2 bytes and the question
  does not arise — so this remains open for widths that are not a whole number of bytes.

## If something fails

| symptom | likely cause |
|---|---|
| `hls_stream.h not under VITIS_INC` | Vitis not installed at the default path — set `VITIS_INC` |
| `xf_blas.hpp not under BLAS_INC` | Vitis Libraries not cloned, or `blas` not in the sparse checkout — set `BLAS_INC` |
| level 2 leaves `git status` dirty | a different library version; check the `PLAN.md` note that `gemv.hpp` and `dotHelper.hpp` are byte-identical between 2023.1 and 2025.1 before assuming a real change |
| `test_the_shipped_kernel_is_wrong` fails | AMD fixed the `ap_fixed` defect — good news, and the signal to retire `gemv_fixed_as_shipped` |
| `test_wider_mac_type_is_uncompilable_in_the_library` fails | AMD fixed `t_MacDataType` — the integer model's single-`width` assumption needs revisiting |
| a generator raises "no discriminating vector" | the test data has stopped separating the model from a plausible wrong one; **enlarge the case, do not lower the bar** |
