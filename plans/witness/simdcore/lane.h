// lane.h -- one cplx4 lane of the SIMD core (plans/simdcore.md, plans/simdcore_records.md D1, D3, D4).
//
// A lane word is 32 bits: a complex 16-bit element (re bits 15:0, im bits 31:16), two e16 reals, or
// one e32 real.  Four 17x17 signed products are shared by every multiplying op:
//     m0 = a.lo * b.lo   m1 = a.hi * b.hi   m2 = a.lo * b.hi   m3 = a.hi * b.lo
// complex:  a b       = (m0 - m1) + j(m2 + m3)      conj(a) b = (m0 + m1) + j(m2 - m3)
// e16 pair: vsmul     = m0 (lo), m1 (hi)
// e32 (SC_E32MUL):  a b = m1 << 32 + (m2 + m3) << 16 + m0, the lo halves zero-extended.
// Rounding: RVV vxrm = 0 (rnu, round half up): (p + 2^(s-1)) >> s.
#pragma once
#include <ap_int.h>

typedef ap_uint<32> word_t;

enum Op {
    CMUL = 0,   // wf.vcmul.vv    vd = rs16(vs2 * vs1)
    CMULC,      // wf.vcmulc.vv   vd = rs16(conj(vs2) * vs1)
    CMACC,      // wf.vcmacc.vv   vd = sat16(vd + rs16(vs2 * vs1))
    CDOTR,      // wf.vcdotr.vv   vd = sat32(rnd(Re(conj(vs2) vs1), rsh))
    ADD16,      // vadd.vv  e16
    SUB16,      // vsub.vv  e16
    MAX16,      // vmax.vv  e16
    MIN16,      // vmin.vv  e16
    SMUL16,     // vsmul.vv e16 (Q15, rnu, saturating)
    SSRA16,     // vssra.vx e16 (shift = x)
    MSLT16,     // vmslt.vv e16 -> mask
    MERGE16,    // vmerge.vvm e16: mask ? vs1 : vs2
    ADD32,      // vadd.vv  e32
    SUB32,      // vsub.vv  e32
    MAX32,      // vmax.vv  e32
    SSRA32,     // vssra.vx e32
    MVX,        // vmv.v.x  e32
    MUL32,      // vmul.vv  e32  (SC_E32MUL)
    MULH32,     // vmulh.vv e32  (SC_E32MUL)
    NOP
};

typedef ap_int<16> s16;
typedef ap_int<17> s17;
typedef ap_int<34> s34;

static inline s16 lo16(word_t w) { s16 r = w.range(15, 0); return r; }
static inline s16 hi16(word_t w) { s16 r = w.range(31, 16); return r; }
static inline word_t pack16(s16 lo, s16 hi) {
    word_t w;
    w.range(15, 0) = lo.range(15, 0);
    w.range(31, 16) = hi.range(15, 0);
    return w;
}

// round half up, then arithmetic shift right by s (0..31)
template <int W>
static inline ap_int<W> rnd(ap_int<W> p, ap_uint<5> s) {
#pragma HLS inline
    ap_int<W + 1> q = p;
    if (s != 0) q += (ap_int<W + 1>(1) << (s - 1));
    return ap_int<W>(q >> s);
}

template <int W>
static inline s16 sat16(ap_int<W> v) {
#pragma HLS inline
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return s16(v);
}

template <int W>
static inline ap_int<32> sat32(ap_int<W> v) {
#pragma HLS inline
    const ap_int<W> mx = 2147483647, mn = -ap_int<W>(2147483647) - 1;
    if (v > mx) return ap_int<32>(2147483647);
    if (v < mn) return ap_int<32>(-2147483647 - 1);
    return ap_int<32>(v);
}

// One lane, one element: op on a = vs2, b = vs1 (or x), c = vd (old); mask in / out for e16 pairs.
static inline word_t lane_op(ap_uint<5> op, word_t a, word_t b, word_t c, word_t x, ap_uint<5> csh,
                             ap_uint<5> rsh, ap_uint<2> mask_in, ap_uint<2> &mask_out) {
#pragma HLS inline
#ifdef SC_FIXSH
    csh = 15;  // the shifts fixed at build: what the variable rounding shifters cost
    rsh = 0;
#endif
    s16 al = lo16(a), ah = hi16(a), bl = lo16(b), bh = hi16(b), cl = lo16(c), chh = hi16(c);
    // the shared multipliers: low halves zero-extended for the e32 product, else sign-extended
#ifdef SC_E32MUL
    bool u = (op == MUL32 || op == MULH32);
    s17 al17 = u ? s17(ap_uint<16>(a.range(15, 0))) : s17(al);
    s17 bl17 = u ? s17(ap_uint<16>(b.range(15, 0))) : s17(bl);
#else
    s17 al17 = al, bl17 = bl;
#endif
    s17 ah17 = ah, bh17 = bh;
    s34 m0 = al17 * bl17, m1 = ah17 * bh17, m2 = al17 * bh17, m3 = ah17 * bl17;

    bool conj = (op == CMULC || op == CDOTR);
    ap_int<35> pre = conj ? ap_int<35>(m0 + m1) : ap_int<35>(m0 - m1);
    ap_int<35> pim = conj ? ap_int<35>(m2 - m3) : ap_int<35>(m2 + m3);
    s16 rre = sat16(rnd<35>(pre, csh)), rim = sat16(rnd<35>(pim, csh));

    word_t r = 0;
    ap_uint<2> mo = 0;
    switch (op) {
    case CMUL:
    case CMULC: r = pack16(rre, rim); break;
    case CMACC: r = pack16(sat16(ap_int<18>(cl) + rre), sat16(ap_int<18>(chh) + rim)); break;
    case CDOTR: r = word_t(sat32(rnd<35>(pre, rsh))); break;
    case ADD16: r = pack16(s16(al + bl), s16(ah + bh)); break;
    case SUB16: r = pack16(s16(al - bl), s16(ah - bh)); break;
    case MAX16: r = pack16(al > bl ? al : bl, ah > bh ? ah : bh); break;
    case MIN16: r = pack16(al < bl ? al : bl, ah < bh ? ah : bh); break;
    case SMUL16: r = pack16(sat16(rnd<34>(m0, 15)), sat16(rnd<34>(m1, 15))); break;
    case SSRA16: {
        ap_uint<5> s = x.range(3, 0);
        r = pack16(rnd<16>(al, s), rnd<16>(ah, s));
        break;
    }
    case MSLT16:
        mo[0] = al < bl;
        mo[1] = ah < bh;
        r = mo;
        break;
    case MERGE16: r = pack16(mask_in[0] ? bl : al, mask_in[1] ? bh : ah); break;
    case ADD32: r = word_t(ap_int<32>(a) + ap_int<32>(b)); break;
    case SUB32: r = word_t(ap_int<32>(a) - ap_int<32>(b)); break;
    case MAX32: r = ap_int<32>(a) > ap_int<32>(b) ? a : b; break;
    case SSRA32: r = word_t(rnd<32>(ap_int<32>(a), x.range(4, 0))); break;
    case MVX: r = x; break;
#ifdef SC_E32MUL
    case MUL32:
    case MULH32: {
        ap_int<66> p = (ap_int<66>(m1) << 32) + (ap_int<66>(ap_int<35>(m2) + ap_int<35>(m3)) << 16) + ap_int<66>(m0);
        r = (op == MUL32) ? word_t(p.range(31, 0)) : word_t(p.range(63, 32));
        break;
    }
#endif
    default: r = c; break;
    }
    mask_out = mo;
    return r;
}
