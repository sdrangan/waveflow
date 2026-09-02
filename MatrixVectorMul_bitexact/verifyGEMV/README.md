# `verifyGEMV/` — the Vitis check

Runs the **real AMD Vitis BLAS `gemv`** through C-simulation, C-synthesis and C/RTL
co-simulation, then compares every result against the Python model in
[`../wf_gemv/`](../wf_gemv/) — bit for bit.

**Result: 9/9 comparisons pass.  121 rows, three DUTs, C-sim and co-sim both bit-exact against
the model, and identical to each other.**

Co-simulation *is* the RTL simulation here: Vitis has no separate RTL-sim step for an
`ap_ctrl_hs` kernel, it drives the synthesized RTL with the same testbench in `xsim`.

## Quick start

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /tools/Xilinx/2025.1/Vivado/settings64.sh
source ../../env/bin/activate
export WF_BLAS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw

vitis-run --mode hls --tcl run.tcl     # csim -> csynth -> cosim, all three DUTs (~4 min)
python verify.py                        # compare everything against the Python model
```

`verify.py` reports honestly on a partly-run flow rather than pretending a skipped stage passed,
and exits non-zero if anything failed or was left empty.

## Three DUTs, three questions

Each top exists because native C-simulation structurally cannot answer its question.

| DUT | overload | question it settles | verdict |
|---|---|---|---|
| `gemv_f32_top` | 5-arg, `float` | does the `dot_tree` reduction survive to RTL unchanged? | ✅ yes, 16/16 rows |
| `gemv_ab_top` | 8-arg, `alpha`/`beta` | **does HLS fuse `axpy`'s `alpha*x + y` into an FMA?** | ✅ **no** — see below |
| `gemv_fixed_top` | 5-arg, `ap_fixed<16,8>` | is the `dotHelper.hpp:98` defect real hardware, or a csim artifact? | ⚠️ **real hardware** |

## ⚠️ The FMA question, answered

`axpy` computes `p_alpha * l_realX + l_realY` as a **single expression** (`axpy.hpp:71`).  A
fused multiply-add keeps the product's full precision and rounds once; a separate multiply and
add round twice.  On native builds this is worth 25 of 576 rows depending on nothing but a
compiler flag — so which one the *hardware* does is not a detail.

Two independent lines of evidence say **Vitis HLS does not fuse it**:

**Numerically** — `verify.py` prints the verdict and, crucially, whether the data could have
produced the opposite one:

```
rows on which the two readings differ at all : 4/96
RTL matches the UNFUSED (separate mul + add) : 96/96
RTL matches the FUSED (single rounding)      : 92/96
=> Vitis HLS does NOT fuse it.
```

The 4 discriminating rows matter more than the 96.  Without them "matches unfused" would be an
accident of the test data, and `verify.py` says `INCONCLUSIVE` rather than claiming a result.

**Structurally** — the synthesis report for `axpy` instantiates **two separate floating-point
cores**, not a fused MAC:

```
fmul_32ns_32ns_32_4_max_dsp_1     3 DSP
fadd_32ns_32ns_32_5_full_dsp_1    2 DSP
```

So `wf_gemv.gemv.axpy`'s unfused reading is what the FPGA does, and the `-ffp-contract=off` pin
in `../tools/regen_golden.sh` matches the hardware rather than merely being a defensible choice.

## ⚠️ The `ap_fixed` defect is in the silicon

`gemv_fixed_top` is expected to be **wrong**, and it is — in RTL as well as in C-simulation.

`dot_dsp` ends with `p_res.write(l_res)`, converting `t_MacDataType` to the stream's `ap_uint<W>`
**by value rather than by bit pattern** (`dotHelper.hpp:98`).  For an integer that conversion is
the identity, which is why the bug stays invisible until `ap_fixed` is instantiated.  For
`ap_fixed` it truncates toward zero to the integer part, which the consumer then unpacks as a raw
stored field:

```
ap_fixed<16,8>, true dot = 27.75  ->  stream carries 27  ->  reads back as 27/256 = 0.105469
```

The model compared against here is `gemv_fixed_as_shipped`, i.e. the model *of the bug*, and it
matches the RTL on 9/9 rows.  This settles that the defect is not a C-simulation artifact.

## Synthesis results

`xc7z020clg484-1`, 10 ns clock.

| DUT | size | latency (cycles) | II | DSP | FF | LUT |
|---|---|---|---|---|---|---|
| `gemv_f32_top` | M=4, N=64, P=4 | 130 | 128 | 20 | 3162 | 4421 |
| `gemv_ab_top` | M=4, N=64, P=4 | 130 | 128 | 28 | 4629 | 6299 |
| `gemv_fixed_top` | M=3, N=32, P=4 | 104 | 96 | 4 | 553 | 932 |

The `alpha`/`beta` overload costs 8 more DSPs than the plain one — the `fmul` + `fadd` pair
above, plus `scal`'s multiplier.  The `ap_fixed` DUT is an order of magnitude smaller because its
MACs are integer, which is the whole reason one would reach for fixed point here.

## Files

```
verifyGEMV/
├── README.md          this file
├── ARCHITECTURE.md    what each DUT is and its internal number formats
├── run.tcl            csim -> csynth -> cosim for all three DUTs, one command
├── verify.py          compares every Vitis output against the Python model
├── gen_input.py       regenerates data/
├── data/              input_f32.txt, input_ab.txt, input_fixed.txt
├── src/
│   ├── gemv_top.hpp       sizes + DUT declarations
│   ├── gemv_top.cpp       the three synthesis tops
│   ├── gemv_tb_common.hpp bit-pattern helpers shared by the testbenches
│   └── gemv_*_tb.cpp      one testbench per DUT, shared by csim and cosim
└── results/           output_{f32,ab,fixed}_{csim,cosim}.txt  -- committed, they are the evidence
```

The Vitis build trees (`gemv_*_proj/`, ~290 MB) and `logs/` are gitignored; everything else is
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
the input file disagrees.  `N=64` with `P=4` gives 16 beats and 4 chunks — the smallest size at
which the library's reduction and a plain binary tree are *different* reductions.  Below 4 chunks
they coincide, so a smaller DUT would pass for a model that had the reduction wrong.
