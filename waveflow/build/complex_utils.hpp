#ifndef WAVEFLOW_COMPLEX_UTILS_HPP
#define WAVEFLOW_COMPLEX_UTILS_HPP

// Vitis HLS complex toolkit for Waveflow ComplexField elements: full-precision arithmetic plus
// construct-from-codes / widen / requantize (the only dependency is streamutils_hls.h, for the
// stored-bits <-> ap_fixed conversion).
//
// Mirrors waveflow/utils/complexutils.py exactly: the **explicit re/im formula at FULL
// PRECISION** (ar*br - ai*bi, ar*bi + ai*br) -- NOT std::complex operator*, which
// FMA-contracts (float) / quantizes the product back to the input format (ap_fixed).  Works
// for both the std::complex<T> element (float / ap_fixed inner) and the wf_cint<W> element
// (ap_int inner; std::complex<ap_int> is non-standard).
//
// The result component type is computed by the native ap_int / ap_fixed / float operators,
// which widen exactly as the Python *_format rules do, so the result grows identically:
//
//   cmult -> (2W+1, 2I+1, signed)     conj -> (W+1, I+1, signed)
//   cadd  -> add_format (int bits +1)  csub -> sub_format (always signed)
//
// The result type is always wide enough to hold the full-precision value, so no rounding /
// overflow occurs -- the Q/O modes are irrelevant to the produced bits, hence bit-exact with
// the composed Python model.

#include <ap_fixed.h>
#include <ap_int.h>
#include <complex>

#include "streamutils_hls.h"   // bits_to_fixed (for cx_from_codes)
#include "wf_cint.h"

// Keep the float explicit formula as three IEEE-rounded ops (ar*br, ai*bi, then subtract) --
// no fused-multiply-add contraction -- so it is bit-exact with cmult_float's naive formula.
#pragma STDC FP_CONTRACT OFF

namespace complex_utils {

// --- element component accessors: std::complex<T> .real()/.imag() vs wf_cint<W> .re/.im ---
template <typename T>
static inline T cu_re(const std::complex<T>& z) { return z.real(); }
template <typename T>
static inline T cu_im(const std::complex<T>& z) { return z.imag(); }
template <int W>
static inline ap_int<W> cu_re(const wf_cint<W>& z) { return z.re; }
template <int W>
static inline ap_int<W> cu_im(const wf_cint<W>& z) { return z.im; }

// --- result element type: same family as the input C, with component type Comp ---
template <typename C, typename Comp>
struct cu_result;
template <typename T, typename Comp>
struct cu_result<std::complex<T>, Comp> { typedef std::complex<Comp> type; };
template <int W, typename Comp>
struct cu_result<wf_cint<W>, Comp> { typedef wf_cint<Comp::width> type; };

template <typename C, typename Comp>
static inline typename cu_result<C, Comp>::type cu_make(const Comp& re, const Comp& im) {
    return typename cu_result<C, Comp>::type(re, im);
}

// The return type of each op is deduced (C++14 `auto`) from its body, so it follows the
// native ap_int / ap_fixed / float operator widening exactly -- the same growth the Python
// *_format rules apply.  (A trailing `decltype(...)` return type is both redundant and, for
// ap_fixed subtraction, mis-deduced -- so the body is the single source of truth.)

// cmult / cadd / csub take **mixed** operand element types (CA, CB): the result type is still
// deduced from the body, so it follows the native ap_int / ap_fixed / float widening for the
// actual operand formats -- the same growth the Python *_format rules apply for unequal inputs.
// Same-type callers (e.g. the complex conformance) just deduce CA == CB; differently-scaled
// operands (e.g. VMAC's alpha[F] * A·op(B)[2F] -> [3F]) compose without an intermediate widen.
// The result family follows the first operand CA (both std::complex / both wf_cint in practice).

// --- cmult: (ar*br - ai*bi) + j(ar*bi + ai*br), full precision ------------------
// Named products (matching cmult's p_rr / p_ii / p_ri / p_ir) keep the op order explicit.
template <typename CA, typename CB>
static inline auto cmult(const CA& a, const CB& b) {
#pragma HLS INLINE
    auto p_rr = cu_re(a) * cu_re(b);
    auto p_ii = cu_im(a) * cu_im(b);
    auto p_ri = cu_re(a) * cu_im(b);
    auto p_ir = cu_im(a) * cu_re(b);
    auto re = p_rr - p_ii;   // sub_format(P, P) -> (2W+1, 2I+1, signed)
    auto im = p_ri + p_ir;   // add_format(P, P) -> same format
    return cu_make<CA>(re, im);
}

// --- cmult3: the same exact product as cmult, with three multiplies (the Gauss form) ----------
//   k1 = br (ar + ai),  k2 = ar (bi - br),  k3 = ai (br + bi);  re = k1 - k3,  im = k1 + k2.
// Every term is exact (the native operators widen), so re and im equal cmult's bit for bit, and
// cmult3 returns them in cmult's own result type, which holds them exactly.  For fixed-point and
// integer elements only: in floating point the three-multiply form rounds differently.
// cmult3_parts works on separate real and imaginary parts, for datapaths that keep them apart
// (the processing elements of a systolic array); the caller's R must hold the exact product.
// Python twin: complexutils.cmult3.
template <typename TA, typename TB, typename R>
static inline void cmult3_parts(const TA& ar, const TA& ai, const TB& br, const TB& bi, R& re,
                                R& im) {
#pragma HLS INLINE
    auto k1 = br * (ar + ai);
    auto k2 = ar * (bi - br);
    auto k3 = ai * (br + bi);
    re = k1 - k3;
    im = k1 + k2;
}

template <typename CA, typename CB>
static inline auto cmult3(const CA& a, const CB& b) {
#pragma HLS INLINE
    auto p = cu_re(a) * cu_re(b);
    auto r = p - p;  // only for its type: cmult's result type, sub_format(P, P)
    decltype(r) re, im;
    cmult3_parts(cu_re(a), cu_im(a), cu_re(b), cu_im(b), re, im);
    return cu_make<CA>(re, im);
}

// --- cadd: (ar+br) + j(ai+bi) (add_format: int bits +1; inner signedness rule) --
template <typename CA, typename CB>
static inline auto cadd(const CA& a, const CB& b) {
#pragma HLS INLINE
    auto re = cu_re(a) + cu_re(b);
    auto im = cu_im(a) + cu_im(b);
    return cu_make<CA>(re, im);
}

// --- csub: (ar-br) + j(ai-bi) (sub_format: always signed) -----------------------
template <typename CA, typename CB>
static inline auto csub(const CA& a, const CB& b) {
#pragma HLS INLINE
    auto re = cu_re(a) - cu_re(b);
    auto im = cu_im(a) - cu_im(b);
    return cu_make<CA>(re, im);
}

// --- conj: ar - j(ai) (result (W+1, I+1, signed); im = 0 - ai, re widened to match) --
template <typename C>
static inline auto conj(const C& a) {
#pragma HLS INLINE
    auto im_in = cu_im(a);
    decltype(im_in) zero = 0;
    auto im = zero - im_in;        // 0 - ai (sub widens to (W+1, I+1, signed)) == cx.conj
    decltype(im) re = cu_re(a);    // widen re losslessly into the same format
    return cu_make<C>(re, im);
}

// --- construct / widen / requantize: the rest of the complex toolkit ------------
// These complete the kit so a datapath stays purely complex-typed -- build an element from its
// stored codes, losslessly widen into a wider accumulator element, and requantize down to an
// output element -- with no hand-split re/im.  (std::complex<ap_fixed> elements.)

// cx_from_codes: build a std::complex<ap_fixed> element from one complex stored-code element
// (the generated integer-inner ComplexField, wf_cint<W> = interleaved re/im stored codes),
// reinterpreting each component's bits as the ap_fixed value -- the bits->ap_fixed counterpart
// of cx_requantize, with no hand-split re/im at the call site.
template <typename CXT, int W>
static inline CXT cx_from_codes(const wf_cint<W>& codes) {
#pragma HLS INLINE
    typedef typename CXT::value_type FX;
    return CXT(streamutils::bits_to_fixed<FX>((ap_uint<W>)codes.re),
               streamutils::bits_to_fixed<FX>((ap_uint<W>)codes.im));
}

// cwiden: value-preserving widen of a complex value into element type ACC -- the lossless
// counterpart to cx_requantize (ACC is wide enough that no rounding / overflow occurs), so a
// binary-point alignment or an accumulate into a wider element is just this widen.
template <typename ACC, typename C>
static inline ACC cwiden(const C& z) {
#pragma HLS INLINE
    typedef typename ACC::value_type ACC_FX;
    return ACC((ACC_FX)z.real(), (ACC_FX)z.imag());
}

// cx_requantize: the single lossy step -- assign the (wide) value into REQ, an ap_fixed with
// the output width/scale + round/saturate modes (== fixputils.quantize), then store the bits in
// the output element type CXO (same width/scale; its modes are irrelevant to the stored bits).
template <typename CXO, typename REQ, typename C>
static inline CXO cx_requantize(const C& z) {
#pragma HLS INLINE
    typedef typename CXO::value_type OUT_FX;
    REQ yr = z.real();
    REQ yi = z.imag();
    return CXO((OUT_FX)yr, (OUT_FX)yi);
}

}  // namespace complex_utils

#endif  // WAVEFLOW_COMPLEX_UTILS_HPP
