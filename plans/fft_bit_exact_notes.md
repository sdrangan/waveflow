# Bit-exact Vitis L1 SSR FFT model — feasibility notes

**Status: NOTES, not a plan.** Captured from a feasibility discussion. The idea is a
standalone, bit-exact Python model of the Vitis DSP **L1 SSR FFT** (fixed-point),
built on PySilicon's `FixedField`/`ComplexField`. Revisit and turn into a real plan
once `ComplexField` lands.

## The idea

Reimplement the Vitis L1 1-D SSR FFT in Python so it produces **bit-identical**
output to the HLS `std::complex<ap_fixed<>>` FFT — i.e. a fast Python model that
predicts the hardware bits without running Vitis. Distinct from "wrap the IP" (see
"Relation to the L1 wrapper" below).

## Why it's valuable (on its own merits)

- **AMD ships no bit-exact Python model.** Their guidance is to build *your own*
  reference (Matlab/Python) as a **float golden for SNR** — not a bit-exact
  fixed-point model. So this fills a real gap, not a duplication. (They could add one
  in a future release; nothing today.)
- **Designers want fast Python that predicts hardware bits** — prototype/verify a
  fixed-point FFT with no Vitis run in the inner loop. Real productivity win.
- It's PySilicon's thesis ("Python = bit-exact source of truth") on a **flagship**
  DSP block, and the first thing to stress complex fixed-point arithmetic end to end.

## Feasibility: tractable, moderate-to-high effort

The hard *foundational* part — bit-exact fixed-point arithmetic — is what
`FixedField`/`ComplexField` provide. The FFT is mostly **composing** those ops in the
structure the **open source** defines (the algorithm is readable; e.g.
`dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_traits.hpp`). The pieces are
bounded and documented:

- **Radix decomposition** — radix 2/4/8/16, `logR(L)` stages. Mechanical.
- **Twiddle factors quantized to 18 bits (16F + 2I)** — a specific documented format
  (sized for the 18×27 DSP multiplier). Replicate the source's cos/sin → `ap_fixed`
  rounding exactly.
- **Three scaling modes** (per-stage bit-growth management): `NO_SCALING` (grow
  `log2(R)`/stage), `GROW_TO_MAX_WIDTH` (grow to 27 bits then saturate), `SCALE`
  (right-shift `log2(R)`/stage). Finite, well-defined.
- **`butterfly_rnd_mode`** — per-butterfly rounding, maps onto `QMode`. The butterfly
  is complex mult + add → `ComplexField.mult`/`add`/`quantize`.

**Key simplification: SSR parallelism doesn't affect the bits.** SSR is a
throughput/layout property (samples-in-parallel-per-cycle); the numerical result is
the same butterflies in the same order. A *sequential* Python model matching the
math + scaling + digit-reversal is bit-identical regardless of the SSR factor — so
model the arithmetic, not the parallelism.

## The two risk corners (where 1-LSB divergence hides)

1. **Twiddle quantization** — matching the source's exact cos/sin → 18-bit `ap_fixed`
   table generation (rounding of the constants).
2. **Per-stage scale / round / saturate** — the exact boundary behavior of the chosen
   scaling mode.

Both are discoverable from the open source and, crucially, **pinned by the same
conformance harness** built for `FixedField` (generate the real Vitis FFT, run csim,
diff bit-for-bit — it reports exactly which sample/stage diverges).

## Relation to the L1 wrapper (roadmap #5)

Complementary, not the same:
- **L1 wrapper (#5):** *call* the vendor IP from a generated kernel; golden = float +
  SNR. Treats the FFT as a black box.
- **This:** *replicate* the FFT bit-exactly in Python; no Vitis needed to predict
  bits.

They reinforce each other: the wrapper/harness **validates** the bit-exact model
(real Vitis FFT vs the Python model, bit-for-bit).

## Prerequisites & sequencing

- Needs `FixedField` (in progress) **and** `ComplexField` (complex-of-`FixedField`,
  next) — the FFT is complex fixed-point throughout.
- Reuses the `FixedField` conformance harness rig (gen Vitis kernel → csim → compare
  bits).
- Natural **capstone** after `ComplexField`. Scope it as its own plan then; the v1
  could target one radix + one scaling mode + a small `L`, then expand.

## Sources
- [L1 SSR FFT user guide (2020.2)](https://xilinx.github.io/Vitis_Libraries/dsp/2020.2/user_guide/L1.html)
- [Vitis_Libraries SSR FFT source](https://github.com/Xilinx/Vitis_Libraries/blob/master/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_traits.hpp)
