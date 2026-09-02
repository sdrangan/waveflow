# `verifyGEMV/` — the Vitis check

Runs the **real AMD Vitis BLAS `gemv`** through C-simulation, C-synthesis and C/RTL
co-simulation, then compares every result against the Python model in
[`../wf_gemv/`](../wf_gemv/) — bit for bit.

**Result: 27/27 comparisons pass.  Nine DUTs, 176 rows, C-sim and co-sim both bit-exact against
the model and identical to each other.**

Co-simulation *is* the RTL simulation here: Vitis has no separate RTL-sim step for an
`ap_ctrl_hs` kernel, it drives the synthesized RTL with the same testbench in `xsim`.

## Quick start

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /tools/Xilinx/2025.1/Vivado/settings64.sh
source ../../env/bin/activate
export WF_BLAS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw

vitis-run --mode hls --tcl run.tcl     # csim -> csynth -> cosim, all nine DUTs (~13 min)
python verify.py                        # compare everything against the Python model
./report.sh                             # the synthesis numbers, straight from the reports
```

`WF_DUTS="i32 u32" vitis-run --mode hls --tcl run.tcl` re-runs a subset, so fixing one DUT does
not cost a full sweep.

`verify.py` reports honestly on a partly-run flow rather than pretending a skipped stage passed,
and exits non-zero if anything failed or was left empty.

## Nine DUTs, nine questions

Each exists because native C-simulation structurally cannot answer its question.

| DUT | what it settles | verdict |
|---|---|---|
| `f32` | does the `dot_tree` reduction survive to RTL? | ✅ 16/16 rows |
| `f32_wide` | is `P=4` special, or does the width generalise? | ✅ 12/12 at `P=8` |
| `f32_pad` | the beat-count **padding** path — no other DUT reaches it | ✅ 8/8 |
| `f64` | **double**, where `AdderDelay` is 8 rather than 4 | ✅ 8/8 |
| `ab` | **does HLS fuse `axpy`'s `alpha*x + y`?** | ✅ **no** |
| `i32` | the `dot_dsp` path — never synthesized before | ✅ 9/9 |
| `u32` | **is `ap_uint<32>` really read unsigned?** | ✅ **yes** |
| `fixed` | is the `ap_fixed` defect real silicon or a csim artifact? | ⚠️ **real silicon** |
| `fix24` | `ap_fixed<24,12>`, where `WideType`'s slot is 32 bits in csim and 24 in RTL | ✅ 9/9, no divergence |

## ⚠️ The FMA question, answered

`axpy` computes `p_alpha * l_realX + l_realY` as a **single expression** (`axpy.hpp:71`).  A
fused multiply-add keeps the product's full precision and rounds once; a separate multiply and
add round twice.  On native builds this is worth 25 of 576 rows depending on nothing but a
compiler flag — so which one the *hardware* does is not a detail.

Two independent lines of evidence say **Vitis HLS does not fuse it**:

```
rows on which the two readings differ at all : 4/96
RTL matches the UNFUSED (separate mul + add) : 96/96
RTL matches the FUSED (single rounding)      : 92/96
=> Vitis HLS does NOT fuse it.
```

and the synthesis report for `axpy` instantiates **two separate cores**:

```
fmul_32ns_32ns_32_4_max_dsp_1     3 DSP
fadd_32ns_32ns_32_5_full_dsp_1    2 DSP
```

The 4 discriminating rows matter more than the 96 that pass — without them "matches unfused"
would be an accident of the data, and `verify.py` prints `INCONCLUSIVE` rather than claiming a
result when that count is zero.

So the `-ffp-contract=off` pin in `../tools/regen_golden.sh` **matches the hardware** rather than
merely being a defensible choice.

## ⚠️ The `ap_fixed` defect is in the silicon

`gemv_fixed_top` and `gemv_fix24_top` are expected to be **wrong**, and they are — in RTL as well
as in C-simulation.

`dot_dsp` ends with `p_res.write(l_res)`, converting `t_MacDataType` to the stream's `ap_uint<W>`
**by value rather than by bit pattern** (`dotHelper.hpp:98`).  For an integer that conversion is
the identity, which is why the bug stays invisible until `ap_fixed` is instantiated:

```
ap_fixed<16,8>, true dot = 27.75  ->  stream carries 27  ->  reads back as 27/256 = 0.105469
```

Both DUTs are compared against `gemv_fixed_as_shipped` — a model **of the bug** — and the defect
changes 9/9 rows in each, so neither would pass for a model that ignored it.

`fix24` also closes a separate worry: `WideType` sizes each slot as `sizeof(T)*8`, which is 32
bits for `ap_fixed<24,12>` in C-simulation and 24 under `__SYNTHESIS__`.  If that leaked into the
data path, csim and cosim would disagree there and nowhere else.  **They do not** — 9/9 identical.

## Signed vs unsigned, and a check that nearly proved nothing

`int32_t` and `ap_uint<32>` are the **same hardware** — identical latency, identical DSP/FF/LUT
(see the table below) — and emit identical **bits**.  Only the value they denote differs.

The first version of this package compared the integer DUTs bit-for-bit, which passes for a
signed model and an unsigned one alike: the `u32` DUT proved nothing.  The testbenches now emit
the decimal **value**, and the check has teeth:

```
rows on which the two readings differ at all : 5/9
RTL matches the UNSIGNED reading             : 9/9
RTL matches the SIGNED reading               : 4/9
```

## Synthesis results

`xc7z020clg484-1`, 10 ns clock.  Regenerate with `./report.sh`.

| DUT | size | latency | II | DSP | FF | LUT |
|---|---|---|---|---|---|---|
| `f32` | M=4, N=64, P=4 | 130 | 128 | 20 | 3162 | 4421 |
| `f32_wide` | M=3, N=128, P=8 | 194 | 192 | 40 | 5464 | 7332 |
| `f32_pad` | M=2, N=208, P=16 | 210 | 208 | 80 | 10125 | 13221 |
| `f64` | M=2, N=64, P=2 | 137 | 64 | 28 | 4026 | 4999 |
| `ab` | M=4, N=64, P=4 | 130 | 128 | 28 | 4629 | 6299 |
| `i32` | M=3, N=32, P=4 | 50 | 48 | 12 | 1739 | 1229 |
| `u32` | M=3, N=32, P=4 | 50 | 48 | 12 | 1739 | 1229 |
| `fixed` | M=3, N=32, P=4 | 104 | 96 | 4 | 553 | 932 |
| `fix24` | M=3, N=32, P=4 | 55 | 48 | 4 | 817 | 1246 |

`i32` and `u32` being **identical to the digit** is the structural half of the signedness result
above.  The float DSP cost scales with the stream width, as expected — the reduction tree is
unrolled.  The fixed-point DUTs are an order of magnitude smaller, which is the whole reason one
would reach for fixed point here, if it worked.

## Files

```
verifyGEMV/
├── README.md          this file
├── ARCHITECTURE.md    what each DUT is and its internal number formats
├── run.tcl            csim -> csynth -> cosim; WF_DUTS selects a subset
├── verify.py          compares every Vitis output against the Python model
├── report.sh          the synthesis numbers, parsed from the csynth reports
├── gen_input.py       regenerates data/
├── data/              one input file per DUT (i32 and u32 share one, on purpose)
├── src/
│   ├── gemv_top.hpp       sizes + DUT declarations
│   ├── gemv_top.cpp       the nine synthesis tops, eight from one macro
│   ├── gemv_tb_common.hpp the file readers, shared
│   ├── gemv_float_tb.cpp  float and double DUTs   (-DWF_DUT=0..3)
│   ├── gemv_intlike_tb.cpp integer and ap_fixed DUTs (-DWF_DUT=0..3)
│   └── gemv_ab_tb.cpp     the alpha/beta DUT
└── results/           output_<dut>_{csim,cosim}.txt -- committed, they are the evidence
```

The Vitis build trees (`*_proj/`, ~100 MB each) and `logs/` are gitignored; everything else is
plain text you can open.

## Two things worth knowing if you extend this

**The tops take arrays, not streams.**  With `hls::stream` as top-level arguments, `csim` and
`csynth` both pass and then co-simulation aborts while instrumenting the testbench:

```
ERROR [HLS SIM]: an hls::stream is read while empty
```

Taking arrays and doing the stream plumbing *inside* the top is the shape the library's own
testbench uses (`L1/tests/hw/gemv/uut_top.cpp`), and it cosims cleanly.

**Sizes are compile-time constants** in `src/gemv_top.hpp`, and the testbenches refuse to run if
the input file disagrees.  They are not arbitrary: below **4 chunks** of `Delays` beats, the
library's reduction and a plain binary tree are the *same* reduction, so a smaller DUT would pass
for a model that had the structure wrong.  `f32_pad` was resized from `N=176` to `N=208` for
exactly that reason — see `ARCHITECTURE.md`.
