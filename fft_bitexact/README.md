# `fft_bitexact/` — what is in here

A bit-exact Python model of the **AMD Vitis DSP L1 SSR FFT** (fixed point): given the same
input bits, it produces the same output bits as the hardware, without running Vitis.

Verified against the shipped library at `L = 16`, `64` and `1024` — the last against synthesized
RTL, 16384 values, zero differences.

**Where to start:** [`VERIFY.md`](VERIFY.md) if you want to run something;
[`PLAN.md`](PLAN.md) if you want to know how it was built and what is still open.

## Directory map

```
fft_bitexact/
├── README.md          this file
├── VERIFY.md          index of the two runnable verification packages
├── PLAN.md            the working record: what was measured, what was wrong, what remains
│
├── wf_fft/            THE MODEL -- the deliverable
├── golden/            reference outputs from the real Vitis library (checked in)
├── cpp/               the generators that produce golden/, + an instrumented header copy
├── tools/             one script that rebuilds every golden
├── tests/             the gates, run with plain pytest -- no Vitis needed
│
├── verifyFFT16/       runnable end-to-end check at L=16
└── verifyFFT1024/     the same at L=1024
```

## `wf_fft/` — the model

The only directory whose contents matter to a *user* of this work.  Pure Python + numpy; it
imports `waveflow` for fixed-point arithmetic and adds no quantization logic of its own.

| file | what it is |
|---|---|
| `fft.py` | the transform.  `fft16` (all three scaling modes, `L=16`) and `fft_general` (`NO_SCALING`, any `L = 4^S`).  The split is a scope statement — see its docstring. |
| `twiddle.py` | the twiddle constants, including the **quarter-wave** reconstruction the hardware actually uses past `L=16` |
| `cxquant.py` | the two complex primitives the butterfly needs: a requantize shim over `waveflow.hw.complexfield.cquantize`, and `complex_multiply`, which is FFT-specific and deliberately did not move into the library |

## `golden/` — reference outputs, checked in

Produced by the **real Vitis library**, never by a reimplementation, and compared as raw stored
integers rather than floats.  Checked in so the tests need neither Vitis nor a compiler.

| file | what it pins |
|---|---|
| `twiddle_L16_R4_W18_I2.json` | the stored twiddle table |
| `quarter_twiddle.json` | the quarter-wave *read* path, at L=16/64/1024 |
| `cxops_d16_2_t18_2.json` | complex requantize and the library's own `complexMultiply` |
| `fft_L16_R4_noscale_natural.json` | the transform, 12 vectors |
| `fft_L64_R4_noscale_natural.json` | three stages of recursion, 6 vectors |
| `fft_L16_R4_modes.json` | all three scaling modes, with per-stage traces |

> These are `*.json` and the repo's root `.gitignore` has a blanket `*.json` rule, so they are
> re-included explicitly.  Without that they vanish from a clone and every test errors with
> `FileNotFoundError` while still passing locally — which is exactly what happened.

## `cpp/` — golden generators

Each `dump_*.cpp` instantiates the shipped library and dumps what *it* produces.  All compile
natively under `g++` against `<ap_fixed.h>`, so regenerating a golden takes milliseconds rather
than a Vitis run.

| file | dumps |
|---|---|
| `dump_twiddle.cpp` | `TwiddleTable` — the stored constants |
| `dump_cxops.cpp` | requantize + `complexMultiply` |
| `dump_fft.cpp` | the L=16 transform, 12 vectors |
| `dump_fft_l64.cpp` | the L=64 transform |
| `dump_modes.cpp` | all three scaling modes, with traces |
| `dump_stages.cpp` | per-stage intermediates (needs `vendor_debug/`) |

### `cpp/vendor_debug/` — an instrumented copy of the Vitis headers

45 headers copied from `Vitis_Libraries`, plus `wf_trace.hpp` and a handful of `WF_TRACE` points
in the butterfly and adder tree.  All guarded by `-DWF_FFT_TRACE`, so the copy is behaviourally
identical to the vendor tree unless that flag is set.

**This is the most valuable thing in the directory after the model itself.**  Every number
format in the model was *measured* with it.  Reading the templates and reasoning about
`ButterflyTraits` produced a confident wrong answer twice; the tracer settled it in minutes.
If you extend the model, instrument first.

The verification packages build against the **pristine** library, never this copy.

## `tools/`

`regen_golden.sh` rebuilds every file in `golden/` from `cpp/`.  Override `VITIS_INC` and `VLIB`
if your installs differ from the defaults.

## `tests/`

Plain `pytest fft_bitexact/tests/` — no Vitis, no compiler, because the goldens are checked in.

| file | gate |
|---|---|
| `test_twiddle.py` | the stored table |
| `test_cxops.py` | requantize and the butterfly multiply |
| `test_fft.py` | golden shape, growth formula, output ordering |
| `test_fft_model.py` | the L=16 transform, all 12 vectors |
| `test_modes.py` | all three scaling modes |
| `test_general.py` | `L = 4^S`, including L=1024 against the RTL output in `verifyFFT1024/` |

Several tests assert that a **plausible wrong model fails** — truncating twiddles instead of
rounding, wrapping one bit wider, dropping `SCALE`'s fractional shift.  A gate that cannot fail
proves nothing, and each of those mistakes was made for real before it was pinned.

## `verifyFFT16/` and `verifyFFT1024/`

Self-contained, hand-runnable packages: Vitis C-simulation → C-synthesis → C/RTL
co-simulation, then a Python comparison.  Identical structure, only `FFT_L` differs.

```
verifyFFT16/
├── README.md          the walkthrough: environment -> vitis-run -> verify.py
├── ARCHITECTURE.md    what the DUT is and its internal number formats
├── run.tcl            csim -> csynth -> cosim, one command
├── verify.py          compares Vitis output against the model
├── src/               fft_top.hpp (config), fft_top.cpp (DUT), fft_tb.cpp (testbench)
├── data/input.txt     input vectors, raw stored integers
└── results/           output_csim.txt, output_cosim.txt
```

`verifyFFT1024/` adds `gen_input.py`, because 1024 samples is not readable by hand.

Both build a Vitis project tree in place (`fft_verify_proj/`, `fft1024_verify_proj/`) which is
gitignored; everything else is plain text you can open.

## Conventions used throughout

* **Goldens come from the vendor's own code**, never a reimplementation — otherwise a test only
  proves two reimplementations agree with each other.
* **Comparisons are on raw stored integers.**  A float comparison absorbs exactly the 1-LSB
  differences this project exists to detect.
* **Measure, don't derive.**  Every format rule here came from the tracer after reasoning had
  failed.  This happened three separate times; it is recorded in `PLAN.md` each time.
