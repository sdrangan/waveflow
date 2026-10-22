// cfg.h -- the witness configuration: the centre (L 4, VLMAX 8) with CG-like formats. Stage 5 will
// generate this struct from a VecUnitConfig; here it is written by hand, one variant per macro set:
//   VX_NG    register groups (8, 16, 32)          VX_NB   banks per lane (4, or VX_NG = a memory a group)
//   VX_NVF   distinct vector formats (5, or 1)    VX_DEP_FALSE   the DEPENDENCE pragma on the pass
#ifndef VX_CFG_H
#define VX_CFG_H
#include <ap_int.h>

#ifndef VX_NG
#define VX_NG 32
#endif
#ifndef VX_NB
#define VX_NB 4
#endif
#ifndef VX_NVF
#define VX_NVF 5
#endif

namespace vx {

// One quantizer: from an exact value with FROM fraction bits to a W-bit format with F fraction bits.
// Floor (AP_TRN) or floor plus the bit below the cut (AP_RND), then wrap or saturate to W bits:
// fixputils.quantize, value for value.
template <int OUTW, int W, int F, bool RND, bool SAT, int FROM, int XW>
ap_int<OUTW> quant(ap_int<XW> x) {
#pragma HLS INLINE
    const int dF = FROM - F;
    ap_int<XW + 1> t;
    if (dF > 0) {
        t = x >> (dF > 0 ? dF : 0);
        if (RND) t += ap_int<XW + 1>((x >> (dF > 1 ? dF - 1 : 0)) & 1);
    } else {
        t = ap_int<XW + 1>(x) << (dF < 0 ? -dF : 0);
    }
    if (SAT) {
        const ap_int<XW + 1> hi = (ap_int<XW + 1>(1) << (W - 1)) - 1;
        const ap_int<XW + 1> lo = -(ap_int<XW + 1>(1) << (W - 1));
        if (t > hi) t = hi;
        if (t < lo) t = lo;
    }
    return ap_int<OUTW>(ap_int<W>(t));      // wrap to W bits, then sign-extend
}

struct Cfg {
    static const int L = 4, VLMAX = 8, NG = VX_NG, NB = VX_NB, NS = 48, S = 3, NQI = 2, NQO = 2;
    static const int KMAX = 8, NGQ = 8, QD = KMAX * NGQ;   // a queue block: Kmax x Nmax/L groups
    static const int WR = 12, WQ = 12, WSR = 20;           // stored vector, queue, scalar widths
    static const int FU = 9, IU = 5;                        // union vector format: (14, 5)
    static const int FS = 11, IS = 10;                      // union scalar format: (21, 10)
    static const int FA = 11;                               // the multiplier's A input: max(FU, FS)
    static const int FP = FA + FU;                          // a product, and a slot's exact result
    static const int GDIV = 6;                              // a dividend's extra fraction bits
    typedef ap_int<FU + IU> vu_t;
    typedef ap_int<FS + IS> su_t;
    typedef ap_int<FA + IS + 1> au_t;                       // a vector or a scalar, negatable
    typedef ap_int<40> res_t;                               // int 18 (a product, a carry, imm) + frac 20
    typedef ap_int<41> dot_t;                               // Re(P): int 17 + log2 VLMAX, frac FP
    static const int WN = WSR + GDIV + FS;                  // a divide's numerator
    typedef ap_int<WN + 1> num_t;
    static const int FQ = FS + GDIV;                        // a quotient aligned to the widest dividend

    // ---- vector formats: (W, F, rnd, sat) ----------------------------------------------------
    // 0: (12, 9) RND SAT   1: (12, 8) RND SAT   2: (12, 7) RND SAT   3: (12, 8) TRN WRAP   4: (12, 9) TRN SAT
    static int gfmt(int g) { return g % VX_NVF; }
    static int qfmt_in(int q) { return q % VX_NVF; }
    static int qfmt_out(int q) { return (q + 1) % VX_NVF; }

    static vu_t conv_v(ap_int<WR> raw, int f) {
#pragma HLS INLINE
        vu_t r = vu_t(raw);
#if VX_NVF > 1
        switch (f) {
        case 1: case 3: r = vu_t(raw) << 1; break;
        case 2: r = vu_t(raw) << 2; break;
        default: break;
        }
#endif
        return r;
    }
    static ap_int<WR> quant_v(res_t x, int f) {
#pragma HLS INLINE
#if defined(VX_QSHARED) && VX_NVF > 1
        // One quantizer, its parameters muxed on the format id: the format's constant shift and round
        // bit, then one rounding adder and one saturator with muxed bounds, and a muxed wrap width.
        ap_int<40 - 7 + 1> sh;      // x >> dF, dF >= FP - 9 = 11 here
        ap_uint<1> rb;
        int w = 12;
        bool rnd, sat;
        switch (f) {
        case 1: sh = x >> 12; rb = x[11]; rnd = true; sat = true; break;
        case 2: sh = x >> 13; rb = x[12]; rnd = true; sat = true; break;
        case 3: sh = x >> 12; rb = x[11]; rnd = false; sat = false; break;
        case 4: sh = x >> 11; rb = x[10]; rnd = false; sat = true; break;
        default: sh = x >> 11; rb = x[10]; rnd = true; sat = true; break;
        }
        ap_int<40 - 7 + 2> t = sh;
        if (rnd) t += rb;
        const ap_int<40 - 7 + 2> hi = (ap_int<40 - 7 + 2>(1) << (w - 1)) - 1, lo = -(hi + 1);
        if (sat && t > hi) t = hi;
        if (sat && t < lo) t = lo;
        return ap_int<WR>(ap_int<12>(t));
#elif VX_NVF > 1
        switch (f) {
        case 1: return quant<WR, 12, 8, true, true, FP>(x);
        case 2: return quant<WR, 12, 7, true, true, FP>(x);
        case 3: return quant<WR, 12, 8, false, false, FP>(x);
        case 4: return quant<WR, 12, 9, false, true, FP>(x);
        default: return quant<WR, 12, 9, true, true, FP>(x);
        }
#else
        return quant<WR, 12, 9, true, true, FP>(x);
#endif
    }

    // ---- scalar formats ------------------------------------------------------------------------
    // 0: (20, 10)   1: (20, 11)   2: (12, 7)   3: (12, 9), all RND SAT
    static int sfmt(int s) { return s % 4; }
    static su_t conv_s(ap_int<WSR> raw, int f) {
#pragma HLS INLINE
        switch (f) {
        case 0: return su_t(raw) << 1;
        case 2: return su_t(raw) << 4;
        case 3: return su_t(raw) << 2;
        default: return su_t(raw);
        }
    }
    template <int FROM, int XW>
    static ap_int<WSR> quant_sf(ap_int<XW> x, int f) {
#pragma HLS INLINE
        switch (f) {
        case 0: return quant<WSR, 20, 10, true, true, FROM>(x);
        case 2: return quant<WSR, 12, 7, true, true, FROM>(x);
        case 3: return quant<WSR, 12, 9, true, true, FROM>(x);
        default: return quant<WSR, 20, 11, true, true, FROM>(x);
        }
    }
    static ap_int<WSR> quant_s(su_t x, int f) { return quant_sf<FS>(x, f); }
    static ap_int<WSR> quant_dot(dot_t x, int f) { return quant_sf<FP>(x, f); }

    // fixputils.div(w(a), b): the integer quotient (a << (GDIV + Fb)) / b, truncated toward zero, has
    // Fa + GDIV fraction bits; aligned to FQ (exact), then quantized to the destination.
    static int sfrac(int f) {
#pragma HLS INLINE
        return f == 0 ? 10 : f == 1 ? 11 : f == 2 ? 7 : 9;
    }
    static ap_int<WSR> div_s(ap_int<WSR> a, int fa, ap_int<WSR> b, int fb, int fd) {
#pragma HLS INLINE
        num_t num = num_t(a);
        switch (fb) {
        case 0: num <<= GDIV + 10; break;
        case 1: num <<= GDIV + 11; break;
        case 2: num <<= GDIV + 7; break;
        default: num <<= GDIV + 9; break;
        }
        const num_t q = num / num_t(b);
        ap_int<WN + 1 + FS - 7> qa = q;         // room for the largest alignment (FS - the least Fa)
        switch (fa) {
        case 0: qa = q << 1; break;
        case 2: qa = q << 4; break;
        case 3: qa = q << 2; break;
        default: break;
        }
        return quant_sf<FQ>(qa, fd);
    }
};

}  // namespace vx

#endif  // VX_CFG_H
