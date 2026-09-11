# `verifyGEMV/` — the Vitis check

Runs the **real AMD Vitis BLAS `gemv`** through C-simulation, C-synthesis and C/RTL
co-simulation, then compares every result against the Python model in
[`../wf_gemv/`](../wf_gemv/) — bit for bit.

**Result: 30/30 comparisons pass.  Ten DUTs, 182 rows, C-sim and co-sim both bit-exact against
the model and identical to each other.**

Co-simulation *is* the RTL simulation here: Vitis has no separate RTL-sim step for an
`ap_ctrl_hs` kernel, it drives the synthesized RTL with the same testbench in `xsim`.

## Quick start

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /tools/Xilinx/2025.1/Vivado/settings64.sh
source ../../env/bin/activate
export WF_BLAS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw

vitis-run --mode hls --tcl run.tcl     # csim -> csynth -> cosim, all ten DUTs (~17 min)
python verify.py                        # compare everything against the Python model
./report.sh                             # the synthesis numbers, straight from the reports
```

`WF_DUTS="i32 u32" vitis-run --mode hls --tcl run.tcl` re-runs a subset, so fixing one DUT does
not cost a full sweep — a single small DUT is about 80 seconds.

`verify.py` reports honestly on a partly-run flow rather than pretending a skipped stage passed,
and exits non-zero if anything failed or was left empty.

## Ten DUTs, ten questions

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
| `user` | **your own data** — see [Bring your own input](#bring-your-own-input) | ✅ 6/6 on the shipped example |

## ⚠️ The FMA question, answered

`axpy` computes `p_alpha * l_realX + l_realY` as a **single expression** (`axpy.hpp:71`).  A
fused multiply-add keeps the product's full precision and rounds once; a separate multiply and
add round twice.  On native builds this is worth 25 of 288 rows depending on nothing but a
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

## Bring your own input

The nine DUTs above use data chosen to stress particular corners.  To check bit-exactness on
**your own numbers**, use the tenth: `user`.

### 1. Write your numbers

Create a plain text file — decimals are fine, you do not convert anything by hand:

```text
# type float
# logp 2
2 3 64
  <case 0: 3*64 matrix entries, row-major>
  <case 0: 64 vector entries>
  <case 1: 3*64 matrix entries>
  <case 1: 64 vector entries>
```

* `# type` — one of `float`, `double`, `int8`/`int16`/`int32`/`int64`, `uint16`/`uint32`, or
  `fixed<W,I>` (e.g. `fixed<16,8>`).  Both directive lines are **required**.
* `# logp` — log2 of the stream width.  `N` must be a multiple of `2**logp`; the kernel asserts
  this, and `make_user_dut.py` refuses rather than letting synthesis produce nonsense.
* `n_cases M N` — then the values.  Whitespace and line breaks are free; only the order matters.

For integer and `fixed<W,I>` types the values are **stored integers**, not reals: a real value
`r` in `fixed<W,I>` is stored as `round(r * 2**(W-I))`.

### 2. Encode, build, run, compare

```bash
python encode_user_input.py my_numbers.txt   # decimals -> data/user_input.txt
python make_user_dut.py                      # -> src/gemv_user_cfg.hpp (sizes + element type)
WF_DUTS=user vitis-run --mode hls --tcl run.tcl   # csim -> csynth -> cosim, ~90 s
python verify.py
```

Expected, for the `user` block:

```
user  (YOUR data -- see 'Bring your own input' in README.md)
    PASS csim vs Python model: 6/6 rows bit-exact
    PASS cosim vs Python model: 6/6 rows bit-exact
    PASS csim vs cosim: identical
```

Those three lines are the whole claim: the **C-simulation** of the shipped library, the
**synthesized RTL** in co-simulation, and the **Python model** all produce the same bits on your
data.  Any `FAIL` prints the offending rows with both values in hex.

### Why bit patterns on disk

`data/user_input.txt` stores floating-point values as IEEE-754 bit patterns rather than as
decimal text.  A `%.17g` round-trip through text can absorb exactly the 1-ULP differences this
whole package exists to detect, so the on-disk format is unambiguous by construction and
`encode_user_input.py` does the conversion once, in one place.

### Things that will bite you

| symptom | cause |
|---|---|
| `N=... is not a multiple of parEntries` | pick a `logp` that divides `N`, or pad `N` with zeros |
| `output_user_*.txt is STALE` | you changed the input but did not re-run the flow — the message gives the exact command |
| `has N values after the header; ... expected` | a row is short, or `n_cases`/`M`/`N` disagree with the data |
| `type ... takes STORED INTEGERS, but found '1.5'` | integer and `fixed` inputs are stored integers; see above |
| everything passes but you expected it not to | see the note `make_user_dut.py` prints about **chunks** — below 4, the reduction shape is not being tested |

### What a small case does *not* prove

`make_user_dut.py` prints the beat and chunk count and warns when it is under four:

```
16 beats / Delays=4 -> 4 chunk(s)
```

Below **4 chunks**, the library's reduction and a plain binary tree are the *same* reduction, so
a passing run tells you the arithmetic is right but says nothing about the tree shape.  For a
structural check use `N >= 4 * Delays * parEntries` (with `Delays` = 4 for float, 8 for double,
1 otherwise).  The script tells you the number to beat.

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
| `user` | whatever your input says | — | — | — | — | — |

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
├── gen_input.py       regenerates the nine shipped inputs (NOT user_input.txt)
├── encode_user_input.py  your decimals   -> data/user_input.txt
├── make_user_dut.py      that file's header -> src/gemv_user_cfg.hpp
├── data/              one input file per DUT (i32 and u32 share one, on purpose)
│                      plus user_input.txt -- yours, and the only one gen_input.py leaves alone
├── src/
│   ├── gemv_top.hpp       sizes + DUT declarations
│   ├── gemv_top.cpp       the nine synthesis tops, eight from one macro
│   ├── gemv_tb_common.hpp the file readers, shared
│   ├── gemv_float_tb.cpp  float and double DUTs   (-DWF_DUT=0..3)
│   ├── gemv_intlike_tb.cpp integer and ap_fixed DUTs (-DWF_DUT=0..3)
│   ├── gemv_ab_tb.cpp     the alpha/beta DUT
│   ├── gemv_user_tb.cpp   the user DUT, driven by the generated config
│   └── gemv_user_cfg.hpp  GENERATED by make_user_dut.py -- do not edit
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
