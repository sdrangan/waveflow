# `tests/vitis_l1/` — conformance apparatus for the bit-exact Vitis L1 models

The models themselves live in **[`waveflow/vitis_l1/`](../../waveflow/vitis_l1/)** and ship in the
wheel.  Everything here is the machinery that proves them bit-exact, and it is deliberately *not*
installed: it carries ~40k lines of golden data plus vendor-derived C++, none of which belongs in
a `pip install`.

```
tests/vitis_l1/
├── fft/     the DSP L1 SSR FFT       (fixed point, R=4, L=4^S)
└── gemv/    the BLAS L1 matrix-vector multiply
```

Each of the two has the same shape:

| | what it is |
|---|---|
| `test_*.py` | the gates.  Plain `pytest`, no Vitis and no compiler — the goldens are checked in |
| `golden/` | reference outputs **from the vendor's own code**, never a reimplementation |
| `data/` | input vectors, as raw stored integers |
| `cpp/` | the dumpers that produce `golden/`, plus `regen_golden.sh` |
| `verify*/` | self-contained Vitis csim → csynth → cosim packages, then a Python comparison |
| `PLAN.md` | the working record: what was measured, what was wrong, what remains |
| `VERIFY.md` | how to run the verification packages by hand |

## Running

```bash
pytest tests/vitis_l1/            # the gates — fast, no toolchain
bash tests/vitis_l1/fft/cpp/regen_golden.sh    # rebuild the goldens from the vendor library
```

`regen_golden.sh` is idempotent: on a correctly configured host it rewrites every golden
byte-for-byte identically.  That is the check that the goldens still describe the library.

### What each side needs

**FFT — nothing but Vitis.**  Vitis ships the DSP library under `tps/xf_dsp/`, verified
code-identical to the upstream `v2025.1_re` tag across all 45 L1 headers (only the copyright-line
glyphs differ).  The script finds it automatically.

**GEMV — a `Vitis_Libraries` checkout.**  Vitis ships `xf_dsp` but *not* `xf_blas`, so there is no
installed copy to fall back on.  The script's header has the sparse-clone command; pass the result
as `BLAS_INC`.

On Windows use the mingw g++ bundled with Vitis
(`$XILINX_VITIS/tps/mingw/10.0.0/win64.o/nt/bin/g++.exe`) and note that the scripts pass
`-std=gnu++14 -D_USE_MATH_DEFINES`, because the vendor's twiddle table uses `M_PI`, which strict
ISO mode hides.

## Two things that are easy to get wrong

**`cpp/overlay/` is not a vendor fork.**  It holds `wf_trace.hpp` plus the *three* vendor headers
that take `WF_TRACE` calls, and is placed ahead of the library on the include path so it shadows
only those three.  Without `-DWF_FFT_TRACE` every `WF_TRACE` expands to `do {} while (0)`, so the
overlay is behaviourally the vendor tree.  The verification packages build against the pristine
library regardless.  Same idea on the GEMV side: `cpp/vendor_patched/dotHelper_patched.hpp` is one
changed line, included ahead of the shipped header so nothing in the vendor tree is edited.

**`gemv/cpp/gen_input_f64.py` is not host-reproducible.**  It scales by `10.0 ** rng.integers(...)`,
and libm `pow` differs in the last bit between glibc and mingw, so ~81 of 3077 input values move by
1–2 ULP across hosts.  The golden is insensitive to it (those are subdominant terms in sums spanning
1e-12…1e12) and the gates pass either way, but a regeneration on a different host will still show
`data/input_f64_M3_N128.txt` as changed.  See `PLAN.md`.

## Conventions these gates are built on

* **Goldens come from the vendor's own code.**  Otherwise a test only proves that two
  reimplementations agree with each other.
* **Comparisons are on raw stored integers.**  A float comparison absorbs exactly the 1-LSB
  differences these models exist to predict.
* **Several gates assert that a plausible *wrong* model fails** — truncating twiddles instead of
  rounding, `numpy.dot` in place of the library's reduction order.  A gate that cannot fail proves
  nothing, and each of those mistakes was made for real before it was pinned.
