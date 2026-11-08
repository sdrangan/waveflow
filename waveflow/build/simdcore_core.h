// simdcore_core.h -- the SIMD core's configuration-independent HLS parts (plans/simdcore.md Stage 5;
// the golden is waveflow/simdcore/model.py).  Each configuration's tasks are generated into
// wf_simdcore_<id>.h (waveflow/simdcore/hls.py), which includes this header.
//
// What is here:
//   * sc_vi        -- a vector instruction as the scalar task forwards it to the vector task;
//   * the vector op codes (SC_*), the per-element arithmetic sc_elem<W> and one lane sc_lane;
//   * sc_scalar<C> -- the scalar task: job burst -> instruction memory, RV32IM + Zicsr + vsetvl*,
//     legality and the budget, the vector instructions forwarded on vq, replies read on rq, the
//     status word at ecall or at the first error (fatal: then the task takes nothing more).
//
// Both tasks are ONE loop each that never exits (free-running).  Compiled with -DSC_STEP the same
// source runs ONE iteration per call (state static), so a C++ testbench can interleave the two tasks
// deterministically without hls::task threads: the "step-mode" C simulation of tests/simdcore.
#ifndef WF_SIMDCORE_CORE_H
#define WF_SIMDCORE_CORE_H

#include <ap_int.h>
#include <hls_stream.h>
#include "streamutils_hls.h"

#ifdef SC_STEP
#define SC_STATE static
#else
#define SC_STATE
#endif

typedef ap_uint<32> sc_word_t;

// The scalar issue loop's II (Stage 5 csynth: 3 does not close 4 ns once the CSR and illegality
// checks feed the next phase).
#ifndef SC_SCALAR_II
#define SC_SCALAR_II 4
#endif

// --- the vector op codes (waveflow.simdcore.hls.VOPS names them, in this order) -------------------
enum sc_vop {
    SC_ADD = 0, SC_SUB, SC_RSUB, SC_MINU, SC_MIN, SC_MAXU, SC_MAX, SC_AND, SC_OR, SC_XOR,
    SC_SADDU, SC_SADD, SC_SSUBU, SC_SSUB, SC_SLL, SC_SMUL, SC_SRL, SC_SRA, SC_SSRL, SC_SSRA,
    SC_MSEQ, SC_MSNE, SC_MSLTU, SC_MSLT, SC_MSLEU, SC_MSLE, SC_MSGTU, SC_MSGT,
    SC_MERGE, SC_MV, SC_MULHU, SC_MUL, SC_MULH, SC_MADD, SC_NMSUB, SC_MACC, SC_NMSAC,
    SC_DIVU, SC_DIV, SC_REMU, SC_REM,
    SC_REDSUM, SC_REDMINU, SC_REDMIN, SC_REDMAXU, SC_REDMAX,
    SC_MAND, SC_MOR, SC_MNAND,
    SC_MVXS, SC_CPOP, SC_FIRST, SC_MVSX,
    SC_CMUL, SC_CMULC, SC_CMACC, SC_CMADD, SC_CDOTR, SC_CCONJ, SC_CDOTRA,
    SC_VLE, SC_VSE, SC_QPOP, SC_QPUSH, SC_VXSAT, SC_JOB,
    SC_NVOP
};

static inline bool sc_is_cplx(ap_uint<7> op) { return op >= SC_CMUL && op <= SC_CDOTRA; }
static inline bool sc_is_red(ap_uint<7> op) { return op >= SC_REDSUM && op <= SC_REDMAX; }

// A forwarded vector instruction.  x: the scalar operand (rs1 / imm), or the DM bank row of a vle /
// vse, or the window of wf.qpop / wf.qpush, or the vxsat CSR access (mode bits 1:0, value bit 2).
// rmask: the registers it reads (the hazard check); wr: it writes vd.
struct sc_vi {
    ap_uint<7> op;
    ap_uint<5> vd, vs1, vs2;
    ap_uint<1> vm, w16, wr, bx;  // bx: operand B is the scalar x (.vx .vi forms, vmv.s.x)
    ap_uint<13> vl;
    ap_uint<32> x;
    ap_uint<2> vxrm;
    ap_uint<5> csh, rsh;
    ap_uint<16> tx;
    ap_uint<32> rmask;
};

// sc_vi as one word on the vq stream (waveflow.simdcore.hls.VQ_BITS): the system flow declares an
// internal channel as hls::stream<ap_uint<W> >.
#define SC_VQ_BITS 136
typedef ap_uint<SC_VQ_BITS> sc_vq_t;

static inline sc_vq_t sc_pack(const sc_vi& v) {
#pragma HLS inline
    sc_vq_t w = 0;
    w.range(6, 0) = v.op;
    w.range(11, 7) = v.vd;
    w.range(16, 12) = v.vs1;
    w.range(21, 17) = v.vs2;
    w[22] = v.vm;
    w[23] = v.w16;
    w[24] = v.wr;
    w[25] = v.bx;
    w.range(38, 26) = v.vl;
    w.range(70, 39) = v.x;
    w.range(72, 71) = v.vxrm;
    w.range(77, 73) = v.csh;
    w.range(82, 78) = v.rsh;
    w.range(98, 83) = v.tx;
    w.range(130, 99) = v.rmask;
    return w;
}

static inline sc_vi sc_unpack(const sc_vq_t& w) {
#pragma HLS inline
    sc_vi v;
    v.op = w.range(6, 0);
    v.vd = w.range(11, 7);
    v.vs1 = w.range(16, 12);
    v.vs2 = w.range(21, 17);
    v.vm = w[22];
    v.w16 = w[23];
    v.wr = w[24];
    v.bx = w[25];
    v.vl = w.range(38, 26);
    v.x = w.range(70, 39);
    v.vxrm = w.range(72, 71);
    v.csh = w.range(77, 73);
    v.rsh = w.range(82, 78);
    v.tx = w.range(98, 83);
    v.rmask = w.range(130, 99);
    return v;
}

// --- arithmetic --------------------------------------------------------------------------------------

// floor(v / 2^d), or RVV's roundoff(v, d) under vxrm (0 rnu, 1 rne, 2 rdn, 3 rod) when round; d < W.
// One right shifter: y = (2 v) >> d holds floor(v / 2^d) above v's bit d-1 (the round bit); the sticky
// bits below it come from a thermometer mask of d (a LUT a bit), not a second shifter.
template <int W>
static inline ap_int<W> sc_rshift(ap_int<W> v, ap_uint<6> d, ap_uint<2> vxrm, bool round) {
#pragma HLS inline
    const ap_int<W + 1> y = (ap_int<W + 1>(v) << 1) >> d;
    const ap_int<W> q = y >> 1;
    const bool bd1 = y[0];
    bool low_nz = false;
    for (int i = 0; i < W - 1; ++i) {
#pragma HLS UNROLL
        low_nz = low_nz || (v[i] && (i + 1 < d));
    }
    bool r;
    switch (vxrm) {
    case 0: r = bd1; break;
    case 1: r = bd1 && (low_nz || q[0]); break;
    case 2: r = false; break;
    default: r = !q[0] && (bd1 || low_nz); break;
    }
    return (round && r) ? ap_int<W>(q + 1) : q;
}

template <int W, int V>
static inline ap_int<W> sc_clip(ap_int<V> v, bool& sat) {
#pragma HLS inline
    const ap_int<V> mx = (ap_int<V>(1) << (W - 1)) - 1;
    const ap_int<V> mn = -(ap_int<V>(1) << (W - 1));
    const bool hi = v > mx, lo = v < mn;  // branch-free: an if here chains the pipeline's merges
    sat = hi || lo;
    return hi ? ap_int<W>(mx) : lo ? ap_int<W>(mn) : ap_int<W>(v);
}

// One element of width W (16 or 32): the OPI / OPM ops, ALU-style -- ONE adder (add, sub, rsub, the
// saturating adds, the multiply-adds, every compare and min / max), ONE right-shift rounding unit
// (vsrl vsra vssrl vssra), one left shifter; prod is the op's product (vs2*vs1, or vs1*vd for vmadd /
// vnmsub), exact, from the lane's shared multipliers.
template <int W>
static inline ap_uint<W> sc_elem(ap_uint<7> op, ap_uint<W> ua, ap_uint<W> ub, ap_uint<W> uc,
                                 ap_int<2 * W + 2> prod, bool mbit, ap_uint<2> vxrm, bool& sat) {
#pragma HLS inline
    sat = false;
    const ap_uint<W> lo = prod.range(W - 1, 0);
    const ap_uint<W> hi = prod.range(2 * W - 1, W);
    // the adder: F + S or F - S on W+2 bits, the operands sign- or zero-extended by the op
    const bool sgn = (op == SC_MIN || op == SC_MAX || op == SC_SADD || op == SC_SSUB || op == SC_MSLT ||
                      op == SC_MSLE || op == SC_MSGT);
    ap_uint<W> F = ua, S = ub;
    bool sub = true;
    switch (op) {
    case SC_ADD: case SC_SADDU: case SC_SADD: sub = false; break;
    case SC_RSUB: F = ub; S = ua; break;
    case SC_MACC: F = uc; S = lo; sub = false; break;
    case SC_NMSAC: F = uc; S = lo; break;
    case SC_MADD: S = lo; sub = false; break;
    case SC_NMSUB: S = lo; break;
    default: break;
    }
    ap_int<W> sF, sS;
    sF = F;
    sS = S;
    const ap_int<W + 2> xf = sgn ? ap_int<W + 2>(sF) : ap_int<W + 2>(F);
    const ap_int<W + 2> xs = sgn ? ap_int<W + 2>(sS) : ap_int<W + 2>(S);
    const ap_int<W + 2> sum = xf + (sub ? ap_int<W + 2>(~xs) : xs) + ap_int<W + 2>(sub ? 1 : 0);
    const bool lt = sum < 0, eq = (sum == 0);
    // the right-shift rounding unit
    const ap_uint<6> sh = ub & (W - 1);
    ap_int<W> sa;
    sa = ua;
    const bool sx = (op == SC_SRA || op == SC_SSRA);
    const ap_int<W + 1> xv = sx ? ap_int<W + 1>(sa) : ap_int<W + 1>(ua);
    const ap_int<W + 1> q = sc_rshift<W + 1>(xv, sh, vxrm, op == SC_SSRL || op == SC_SSRA);
    // the saturating results and their flags, as values (no assignment inside a case)
    const bool ovu = sum > ap_int<W + 2>(~ap_uint<W>(0));
    bool scl = false, ssm = false;
    const ap_uint<W> cl = sc_clip<W, W + 2>(sum, scl);
    const ap_uint<W> sm = sc_clip<W, 2 * W + 2>(sc_rshift<2 * W + 2>(prod, W - 1, vxrm, true), ssm);
    sat = (op == SC_SADDU && ovu) || (op == SC_SSUBU && lt) || ((op == SC_SADD || op == SC_SSUB) && scl) ||
          (op == SC_SMUL && ssm);
    ap_uint<W> r = 0;
    switch (op) {
    case SC_ADD: case SC_SUB: case SC_RSUB: case SC_MACC: case SC_NMSAC: case SC_MADD: case SC_NMSUB:
        r = sum.range(W - 1, 0); break;
    case SC_SADDU: r = ovu ? ap_uint<W>(~ap_uint<W>(0)) : ap_uint<W>(sum.range(W - 1, 0)); break;
    case SC_SSUBU: r = lt ? ap_uint<W>(0) : ap_uint<W>(sum.range(W - 1, 0)); break;
    case SC_SADD: case SC_SSUB: r = cl; break;
    case SC_MINU: case SC_MIN: r = lt ? ua : ub; break;
    case SC_MAXU: case SC_MAX: r = lt ? ub : ua; break;
    case SC_MSEQ: r = eq; break;
    case SC_MSNE: r = !eq; break;
    case SC_MSLTU: case SC_MSLT: r = lt; break;
    case SC_MSLEU: case SC_MSLE: r = lt || eq; break;
    case SC_MSGTU: case SC_MSGT: r = !(lt || eq); break;
    case SC_SLL: r = ua << sh; break;
    case SC_SRL: case SC_SRA: case SC_SSRL: case SC_SSRA: r = q.range(W - 1, 0); break;
    case SC_SMUL: r = sm; break;
    case SC_MUL: r = lo; break;
    case SC_MULH: case SC_MULHU: r = hi; break;
    case SC_AND: r = ua & ub; break;
    case SC_OR: r = ua | ub; break;
    case SC_XOR: r = ua ^ ub; break;
    case SC_MERGE: r = mbit ? ub : ua; break;
    case SC_MV: r = ub; break;
    case SC_MAND: r = ua[0] & ub[0]; break;
    case SC_MOR: r = ua[0] | ub[0]; break;
    case SC_MNAND: r = !(ua[0] & ub[0]); break;
    default: r = uc; break;
    }
    return r;
}

static inline ap_int<16> sc_lo16(sc_word_t w) { ap_int<16> r; r = w.range(15, 0); return r; }
static inline ap_int<16> sc_hi16(sc_word_t w) { ap_int<16> r; r = w.range(31, 16); return r; }
static inline sc_word_t sc_pack16(ap_uint<16> lo, ap_uint<16> hi) {
    sc_word_t w;
    w.range(15, 0) = lo;
    w.range(31, 16) = hi;
    return w;
}

// One lane, one 32-bit word: an e32 element, two e16 elements (halves), or a complex pair.  act: the
// active elements (bit 0: e32 / the low half; bit 1: the high half); inactive ones keep c.  mb: the
// v0 mask bits of the same elements (vmerge's select).  Four shared 17 x 17 products (Stage 1 lane.h):
// m0 = P.lo Q.lo, m1 = P.hi Q.hi, m2 = P.lo Q.hi, m3 = P.hi Q.lo with P = vs2 (or vd for the madd
// family) and Q = vs1 / the scalar.
template <bool E16>
static inline sc_word_t sc_lane(ap_uint<7> op, bool w16, sc_word_t a, sc_word_t b, sc_word_t c,
                                ap_uint<2> mb, ap_uint<2> act, ap_uint<2> vxrm, ap_uint<5> csh,
                                ap_uint<5> rsh, bool& sat) {
#pragma HLS inline
    const bool cplx = sc_is_cplx(op);
    const bool pc = (op == SC_CMADD || op == SC_MADD || op == SC_NMSUB);
    const sc_word_t P = pc ? c : a;
    const bool uns = (op == SC_MULHU);
    // low halves: signed for complex and e16 signed ops; zero-extended for e32 (the low half of a
    // 32-bit product) and for unsigned ops.  High halves: signed unless unsigned.
    const bool lo_signed = cplx || (w16 && !uns);
    const bool hi_signed = !uns;
    ap_int<17> plo = lo_signed ? ap_int<17>(sc_lo16(P)) : ap_int<17>(ap_uint<16>(P.range(15, 0)));
    ap_int<17> qlo = lo_signed ? ap_int<17>(sc_lo16(b)) : ap_int<17>(ap_uint<16>(b.range(15, 0)));
    ap_int<17> phi = hi_signed ? ap_int<17>(sc_hi16(P)) : ap_int<17>(ap_uint<16>(P.range(31, 16)));
    ap_int<17> qhi = hi_signed ? ap_int<17>(sc_hi16(b)) : ap_int<17>(ap_uint<16>(b.range(31, 16)));
    const ap_int<34> m0 = plo * qlo, m1 = phi * qhi, m2 = plo * qhi, m3 = phi * qlo;

    // the complex result
    const ap_int<16> ar = sc_lo16(a), ai = sc_hi16(a), cr = sc_lo16(c), ci = sc_hi16(c);
    const bool conj = (op == SC_CMULC || op == SC_CDOTR || op == SC_CDOTRA);
    const ap_int<35> pr = conj ? ap_int<35>(m0 + m1) : ap_int<35>(m0 - m1);
    const ap_int<35> pi = conj ? ap_int<35>(m2 - m3) : ap_int<35>(m2 + m3);
    bool s0 = false, s1 = false, s2 = false, s3 = false, s4 = false, s5 = false, s6 = false;
    const bool dot = (op == SC_CDOTR || op == SC_CDOTRA);
    const ap_int<35> ra = sc_rshift<35>(pr, dot ? rsh : csh, vxrm, true);  // two rounding units
    const ap_int<35> rb = sc_rshift<35>(pi, csh, vxrm, true);
    const ap_int<16> rr = sc_clip<16, 35>(ra, s0);
    const ap_int<16> ri = sc_clip<16, 35>(rb, s1);
    // the multiply-adds' two 16-bit adds: c + p (vcmacc) or p + a (vcmadd)
    const ap_int<16> er = (op == SC_CMACC) ? cr : ar, ei = (op == SC_CMACC) ? ci : ai;
    const ap_int<16> mr = sc_clip<16, 18>(ap_int<18>(rr) + er, s2);
    const ap_int<16> mi = sc_clip<16, 18>(ap_int<18>(ri) + ei, s3);
    const ap_int<16> nc = sc_clip<16, 18>(-ap_int<18>(ai), s4);
    const ap_int<32> dt = sc_clip<32, 35>(ra, s5);
    ap_int<32> cv;
    cv = c;
    const ap_int<32> da = sc_clip<32, 34>(ap_int<34>(dt) + cv, s6);
    const bool mul = (op == SC_CMUL || op == SC_CMULC), mad = (op == SC_CMACC || op == SC_CMADD);
    const sc_word_t cres = mul ? sc_pack16(rr, ri) : mad ? sc_pack16(mr, mi) : (op == SC_CCONJ) ? sc_pack16(ar, nc)
                         : (op == SC_CDOTRA) ? sc_word_t(da) : sc_word_t(dt);
    const bool csat = mul ? (s0 || s1) : mad ? (s0 || s1 || s2 || s3) : (op == SC_CCONJ) ? s4
                    : (op == SC_CDOTRA) ? (s5 || s6) : s5;
    // the element result: one e32, or two e16 (E16 builds only)
    const ap_int<66> p32 = (ap_int<66>(m1) << 32) + (ap_int<66>(ap_int<35>(m2) + m3) << 16) + ap_int<66>(m0);
    bool e0 = false, e1 = false, e2 = false;
    const ap_uint<32> v32 = sc_elem<32>(op, a, b, c, p32, mb[0], vxrm, e0);
    sc_word_t ew = act[0] ? sc_word_t(v32) : c;
    bool esat = act[0] && e0;
    if (E16) {
        const ap_uint<16> lo = sc_elem<16>(op, a.range(15, 0), b.range(15, 0), c.range(15, 0), m0, mb[0], vxrm, e1);
        const ap_uint<16> hi = sc_elem<16>(op, a.range(31, 16), b.range(31, 16), c.range(31, 16), m1, mb[1], vxrm, e2);
        const sc_word_t w16w = sc_pack16(act[0] ? lo : ap_uint<16>(c.range(15, 0)), act[1] ? hi : ap_uint<16>(c.range(31, 16)));
        ew = w16 ? w16w : ew;
        esat = w16 ? ((act[0] && e1) || (act[1] && e2)) : esat;
    }
    sat = cplx ? (act[0] && csat) : esat;
    const sc_word_t r = cplx ? (act[0] ? cres : c) : ew;
    return r;
}

// --- the divider (the vdiv build option) ---------------------------------------------------------------

// One element's vdivu / vdiv / vremu / vrem (op: bit 0 signed, bit 1 remainder), RVV's results for a
// zero divisor (quotient all ones, remainder the dividend) and for the signed overflow (quotient the
// dividend, remainder 0); model._vexec.
template <int W>
static inline ap_uint<W> sc_divw(ap_uint<W> ua, ap_uint<W> ub, ap_uint<2> op) {
#pragma HLS inline
    const bool sgn = op[0], rem = op[1];
    ap_int<W> sa, sb;
    sa = ua;
    sb = ub;
    const bool zero = (ub == 0);
    const bool ovf = sgn && ua == (ap_uint<W>(1) << (W - 1)) && sb == -1;
    ap_uint<W> q, r;
    if (sgn) {
        const ap_int<W + 1> d = zero ? ap_int<W + 1>(1) : ap_int<W + 1>(sb);
        const ap_int<W + 1> qq = ap_int<W + 1>(sa) / d;
        q = qq;
        r = ap_int<W + 1>(ap_int<W + 1>(sa) - qq * d);
    } else {
        const ap_uint<W> d = zero ? ap_uint<W>(1) : ub;
        q = ua / d;
        r = ua - q * d;
    }
    if (zero) return rem ? ua : ap_uint<W>(~ap_uint<W>(0));
    if (ovf) return rem ? ap_uint<W>(0) : ua;
    return rem ? r : q;
}

// A lane word's divides: one e32 element or two e16.
static inline sc_word_t sc_div_lane(sc_word_t a, sc_word_t b, ap_uint<2> op, bool w16) {
#pragma HLS inline
    sc_word_t r;
    if (w16) {
        r.range(15, 0) = sc_divw<16>(a.range(15, 0), b.range(15, 0), op);
        r.range(31, 16) = sc_divw<16>(a.range(31, 16), b.range(31, 16), op);
    } else {
        r = sc_divw<32>(a, b, op);
    }
    return r;
}

// --- the scalar task -----------------------------------------------------------------------------------

// Status codes (waveflow.simdcore.unit.Status).
enum sc_status { SC_OK = 0, SC_ILLEGAL = 1, SC_ADDR = 2, SC_BUDGET = 3, SC_PC = 4 };
// Scalar instruction classes (waveflow.simdcore.hls.SKIND names them, in this order).
enum sc_skind {
    SK_BAD = 0, SK_LUI, SK_AUIPC, SK_JAL, SK_JALR, SK_BR, SK_LOAD, SK_STORE, SK_OPI, SK_OPR, SK_DIV,
    SK_CSR, SK_VSET, SK_ECALL, SK_VEC, SK_VMEM, SK_WFQ
};
// Vector operand forms.
enum sc_form { F_VV = 0, F_VX = 1, F_VI = 2, F_VU = 3, F_NONE = 4 };
// Scalar task phases.
enum sc_phase { SCP_HDR = 0, SCP_ARGS, SCP_PROG, SCP_RUN, SCP_WAIT, SCP_DIV, SCP_HALT };
// CSR addresses.
#define SC_CSR_VSTART 0x008
#define SC_CSR_VXSAT 0x009
#define SC_CSR_VXRM 0x00A
#define SC_CSR_VCSR 0x00F
#define SC_CSR_CYCLE 0xC00
#define SC_CSR_VL 0xC20
#define SC_CSR_VTYPE 0xC21
#define SC_CSR_VLENB 0xC22
#define SC_CSR_WFSH 0x800

// The scalar task.  C: the configuration (generated): L, IMEM, DMB, VLMAX16, VLMAX32, VLENB, the
// decoder dec(), the tables kind() form() vop() wr() rd_vs1() rd_vs2() rd_v0() reply() cplx() eew(),
// and the windows' tables nwin() win_in() win_mask() in_mask().
template <class C, typename PROG_T, typename ST_T>
static void sc_scalar(hls::stream<PROG_T>& s_prog, hls::stream<ST_T>& m_status,
                      hls::stream<sc_vq_t>& vq, hls::stream<ap_uint<32> >& rq) {
    SC_STATE ap_uint<32> imem[C::IMEM];
    // each instruction pre-decoded as it is loaded (C::pre: kind, form, op, flags), so the decoder is
    // off the issue loop's critical path
    SC_STATE ap_uint<32> pdec[C::IMEM];
    SC_STATE ap_uint<32> pw = 0;  // SCP_PROG: a word's second instruction
    SC_STATE bool hb = false;     // SCP_PROG: the second is next
    // the scalar registers: a LUTRAM, and a valid bit each (a job starts with all of them 0)
    SC_STATE ap_uint<32> xr[32];
#pragma HLS BIND_STORAGE variable = xr type = ram_2p impl = lutram
    SC_STATE ap_uint<32> xv = 0;
    SC_STATE ap_uint<3> phase = SCP_HDR;
    SC_STATE ap_uint<32> pc = 0, steps = 0, budget = 0, cnt = 0, ninstr = 0, nargs = 0, nwords = 0;
    SC_STATE ap_uint<16> tx = 0;
    SC_STATE ap_uint<13> vl = 0;
    SC_STATE ap_uint<32> vtype = 0x80000000;
    SC_STATE ap_uint<2> vxrm = 0;
    SC_STATE ap_uint<13> wfsh = 15;
    SC_STATE ap_uint<5> wrd = 0;           // SCP_WAIT / SCP_DIV: the destination
    SC_STATE ap_uint<2> wkind = 0;         // SCP_WAIT: 0 plain, 1 vcsr (old = vxrm << 1 | reply)
    SC_STATE ap_uint<2> wvxrm = 0;         // SCP_WAIT vcsr: vxrm's old value
    SC_STATE ap_uint<2> nvxrm = 0;         // SCP_WAIT vcsr: vxrm's new value
    SC_STATE ap_uint<32> dq = 0, dr = 0, dd = 0;  // SCP_DIV: quotient, remainder, divisor magnitudes
    SC_STATE ap_uint<6> dn = 0;
    SC_STATE ap_uint<3> dop = 0;           // funct3 of the divide
    SC_STATE ap_uint<32> da = 0, db = 0;   // its operands
    SC_STATE ap_uint<32> cyc = 0;
#ifndef SC_STEP
#pragma HLS reset variable = phase
SCALAR:
    for (;;) {
#pragma HLS PIPELINE II = SC_SCALAR_II
#pragma HLS dependence variable = imem inter false
#endif
        cyc++;
        ap_uint<8> status = SC_OK;
        bool finish = false;
        sc_vi qv;  // the vector instruction this iteration forwards (qsend), if any
        qv.op = SC_JOB; qv.vd = 0; qv.vs1 = 0; qv.vs2 = 0; qv.vm = 1; qv.w16 = 0; qv.wr = 0; qv.bx = 0;
        qv.vl = 0; qv.x = 0; qv.vxrm = 0; qv.csh = 0; qv.rsh = 0; qv.tx = 0; qv.rmask = 0;
        bool qsend = false;
        PROG_T pin;
        bool got = false;
        bool xwe = false;  // this iteration's scalar register write, one site (below)
        ap_uint<5> xwa = 0;
        ap_uint<32> xwd = 0;
        if (phase == SCP_HDR || phase == SCP_ARGS || (phase == SCP_PROG && !hb)) got = s_prog.read_nb(pin);
        switch (phase) {
        case SCP_HDR: {
            if (got) {
                const ap_uint<64> h = pin.data;
                tx = h.range(15, 0);
                ninstr = h.range(31, 16);
                nargs = h.range(35, 32);
                budget = h.range(63, 36);
                nwords = (ap_uint<32>(h.range(31, 16)) + 1) >> 1;
                xv = 0;
                pc = 0;
                steps = 0;
                cnt = 0;
                vl = 0;
                vtype = 0x80000000;
                vxrm = 0;
                wfsh = 15;
                qsend = true;  // SC_JOB: the vector task clears vxsat
                phase = (h.range(35, 32) != 0) ? ap_uint<3>(SCP_ARGS)
                        : (nwords != 0) ? ap_uint<3>(SCP_PROG) : ap_uint<3>(SCP_RUN);
            }
        } break;
        case SCP_ARGS: {
            if (got) {
                const ap_uint<64> d = pin.data;
                xwe = true;
                xwa = 10 + cnt.range(2, 0);
                xwd = d.range(31, 0);
                cnt++;
                if (cnt == nargs) {
                    cnt = 0;
                    phase = (nwords != 0) ? SCP_PROG : SCP_RUN;
                }
            }
        } break;
        case SCP_PROG: {
            // one instruction an iteration: a word's first, then its second
            bool doit = false;
            ap_uint<32> x = pw, a = (cnt << 1) + 1;
            if (!hb) {
                if (got) {
                    const ap_uint<64> d = pin.data;
                    x = d.range(31, 0);
                    pw = d.range(63, 32);
                    a = cnt << 1;
                    doit = true;
                    hb = true;
                }
            } else {
                doit = true;
                hb = false;
                cnt++;
                if (cnt == nwords) phase = SCP_RUN;
            }
            if (doit && a < C::IMEM) {
                imem[a] = x;
                pdec[a] = C::pre(x);
            }
        } break;
        case SCP_WAIT: {
            ap_uint<32> v;
            if (rq.read_nb(v)) {
                ap_uint<32> val = v;
                if (wkind == 1) {
                    val = (ap_uint<32>(wvxrm) << 1) | v;
                    vxrm = nvxrm;
                }
                if (wrd != 0) { xwe = true; xwa = wrd; xwd = val; }
                pc += 4;
                phase = SCP_RUN;
            }
        } break;
        case SCP_DIV: {
            // one quotient bit an iteration, restoring, on the magnitudes (div_cycles in the model)
            const ap_uint<33> t = (ap_uint<33>(dr) << 1) | dq[31];
            dq = dq << 1;
            if (t >= dd) {
                dr = t - dd;
                dq[0] = 1;
            } else {
                dr = t;
            }
            dn++;
            if (dn == 32) {
                const bool sgn = (dop == 4 || dop == 6);
                const bool na = sgn && da[31], nb = sgn && db[31];
                ap_uint<32> q = (na != nb) ? ap_uint<32>(-dq) : dq;
                ap_uint<32> rm = na ? ap_uint<32>(-dr) : dr;
                ap_uint<32> res;
                if (db == 0) {
                    res = (dop == 4 || dop == 5) ? ap_uint<32>(0xFFFFFFFF) : da;
                } else if (sgn && da == 0x80000000 && db == 0xFFFFFFFF) {
                    res = (dop == 4) ? ap_uint<32>(0x80000000) : ap_uint<32>(0);
                } else {
                    res = (dop == 4 || dop == 5) ? q : rm;
                }
                if (wrd != 0) { xwe = true; xwa = wrd; xwd = res; }
                pc += 4;
                phase = SCP_RUN;
            }
        } break;
        case SCP_RUN: {
            const ap_uint<32> w = imem[pc.range(31, 2)];
            const ap_uint<32> pd = pdec[pc.range(31, 2)];
            const ap_uint<5> rd = w.range(11, 7), rs1 = w.range(19, 15), rs2 = w.range(24, 20);
            const ap_uint<3> f3 = w.range(14, 12);
            const ap_uint<7> f7 = w.range(31, 25);
            const ap_uint<32> a = xv[rs1] ? xr[rs1] : ap_uint<32>(0);
            const ap_uint<32> b = xv[rs2] ? xr[rs2] : ap_uint<32>(0);
            ap_int<32> sa, sb;
            sa = a;
            sb = b;
            const ap_int<12> immi = w.range(31, 20);
            ap_int<12> imms;
            imms.range(11, 5) = w.range(31, 25);
            imms.range(4, 0) = w.range(11, 7);
            ap_int<13> immb;
            immb[12] = w[31];
            immb[11] = w[7];
            immb.range(10, 5) = w.range(30, 25);
            immb.range(4, 1) = w.range(11, 8);
            immb[0] = 0;
            ap_int<21> immj;
            immj[20] = w[31];
            immj.range(19, 12) = w.range(19, 12);
            immj[11] = w[20];
            immj.range(10, 1) = w.range(30, 21);
            immj[0] = 0;
            const ap_uint<5> kind = pd.range(4, 0);
            const ap_uint<3> form = pd.range(7, 5);
            const ap_uint<7> vop = pd.range(14, 8);
            const bool p_wr = pd[15], p_rd_vs1 = pd[16], p_rd_vs2 = pd[17], p_reply = pd[18], p_cplx = pd[19];
            const ap_uint<6> eew = pd.range(25, 20);
            const ap_uint<1> vill = vtype[31];
            const bool w16cur = (vtype.range(5, 3) == 1);
            const ap_uint<5> vdf = rd;
            ap_uint<32> npc = pc + 4;
            bool we = false;
            ap_uint<32> val = 0;
            bool wait = false, isdiv = false;
            if (pc[0] || pc[1] || pc >= (ninstr << 2)) {
                status = SC_PC;
                finish = true;
            } else if (budget != 0 && steps >= budget) {
                status = SC_BUDGET;
                finish = true;
            } else if (kind == SK_BAD) {
                status = SC_ILLEGAL;
                finish = true;
            } else if (p_wr && ((C::in_mask() >> vdf) & 1)) {
                status = SC_ILLEGAL;  // vd in an input window (records D13)
                finish = true;
            } else {
                steps++;
                switch (kind) {
                case SK_LUI: we = true; val = w & 0xFFFFF000; break;
                case SK_AUIPC: we = true; val = pc + (w & 0xFFFFF000); break;
                case SK_JAL: we = true; val = pc + 4; npc = pc + immj; break;
                case SK_JALR: we = true; val = pc + 4; npc = (a + immi) & ~ap_uint<32>(1); break;
                case SK_BR: {
                    bool tk = false;
                    switch (f3) {
                    case 0: tk = a == b; break;
                    case 1: tk = a != b; break;
                    case 4: tk = sa < sb; break;
                    case 5: tk = sa >= sb; break;
                    case 6: tk = a < b; break;
                    default: tk = a >= b; break;
                    }
                    if (tk) npc = pc + immb;
                } break;
                case SK_OPI:
                case SK_OPR: {
                    // one ALU for the register and the immediate forms (and one multiplier)
                    we = true;
                    const bool imm = (kind == SK_OPI);
                    ap_uint<32> ui;
                    ui = ap_int<32>(immi);
                    const ap_uint<32> o2 = imm ? ui : b;
                    ap_int<32> so2;
                    so2 = o2;
                    const ap_uint<5> sh = imm ? ap_uint<5>(w.range(24, 20)) : ap_uint<5>(b.range(4, 0));
                    if (!imm && f7 == 1) {
                        const bool us = (f3 == 3);  // mulhu
                        const ap_int<33> ma = us ? ap_int<33>(a) : ap_int<33>(sa);
                        const ap_int<33> mb = us ? ap_int<33>(b) : ap_int<33>(sb);
                        const ap_int<66> pr = ma * mb;
                        val = (f3 == 0) ? ap_uint<32>(pr.range(31, 0)) : ap_uint<32>(pr.range(63, 32));
                    } else {
                        switch (f3) {
                        case 0: val = (!imm && w[30]) ? ap_uint<32>(a - o2) : ap_uint<32>(a + o2); break;
                        case 1: val = a << sh; break;
                        case 2: val = sa < so2; break;
                        case 3: val = a < o2; break;
                        case 4: val = a ^ o2; break;
                        case 5: val = w[30] ? ap_uint<32>(sa >> sh) : ap_uint<32>(a >> sh); break;
                        case 6: val = a | o2; break;
                        default: val = a & o2; break;
                        }
                    }
                } break;
                case SK_DIV: {
                    isdiv = true;
                    const bool sgn = (f3 == 4 || f3 == 6);
                    da = a;
                    db = b;
                    dop = f3;
                    dq = (sgn && a[31]) ? ap_uint<32>(-a) : a;
                    dd = (sgn && b[31]) ? ap_uint<32>(-b) : b;
                    dr = 0;
                    dn = 0;
                    wrd = rd;
                } break;
                case SK_ECALL: finish = true; break;
                case SK_CSR: {
                    const ap_uint<12> csr = w.range(31, 20);
                    const bool imm = f3[2];
                    const ap_uint<32> src = imm ? ap_uint<32>(rs1) : a;
                    const bool write = (f3 == 1 || f3 == 5) || rs1 != 0;
                    const ap_uint<2> op2 = f3.range(1, 0);  // 1 rw, 2 rs, 3 rc
                    ap_uint<32> old = 0;
                    bool known = true, ro = false, viax = false;
                    switch (csr) {
                    case SC_CSR_VSTART: old = 0; break;
                    case SC_CSR_VXSAT: viax = true; break;
                    case SC_CSR_VXRM: old = vxrm; break;
                    case SC_CSR_VCSR: viax = true; break;
                    case SC_CSR_CYCLE: old = cyc; ro = true; break;
                    case SC_CSR_VL: old = vl; ro = true; break;
                    case SC_CSR_VTYPE: old = vtype; ro = true; break;
                    case SC_CSR_VLENB: old = C::VLENB; ro = true; break;
                    case SC_CSR_WFSH: old = wfsh; break;
                    default: known = false; break;
                    }
                    const ap_uint<32> nw = (op2 == 1) ? src : (op2 == 2) ? ap_uint<32>(old | src)
                                                                           : ap_uint<32>(old & ~src);
                    if (!known || (write && ro) || (write && csr == SC_CSR_VSTART && op2 != 3 && src != 0)) {  // vstart: old 0
                        status = SC_ILLEGAL;
                        finish = true;
                    } else if (viax) {
                        // vxsat lives in the vector task: a round trip
                        const ap_uint<32> s = src;
                        qv.op = SC_VXSAT;
                        qv.x = write ? ap_uint<32>(op2 | (ap_uint<32>(s[0]) << 2)) : ap_uint<32>(0);
                        qsend = true;
                        wait = true;
                        wrd = rd;
                        wkind = (csr == SC_CSR_VCSR) ? 1 : 0;
                        wvxrm = vxrm;
                        const ap_uint<2> sv = s.range(2, 1);
                        nvxrm = !write ? vxrm : (op2 == 1) ? sv : (op2 == 2) ? ap_uint<2>(vxrm | sv)
                                                                             : ap_uint<2>(vxrm & ~sv);
                    } else {
                        if (write) {
                            if (csr == SC_CSR_VXRM) vxrm = nw.range(1, 0);
                            if (csr == SC_CSR_WFSH) wfsh = nw & 0x1F1F;
                        }
                        we = true;
                        val = old;
                    }
                } break;
                case SK_VSET: {
                    ap_uint<32> vt;
                    const bool isvl = w[31] && !w[30];
                    const bool isi = w[31] && w[30];
                    if (isvl) vt = b;
                    else if (isi) vt = w.range(29, 20);
                    else vt = w.range(30, 20);
                    const ap_uint<3> vsew = vt.range(5, 3);
                    const bool ok = (vsew == 1 && C::SEW16) || (vsew == 2 && C::SEW32);
                    if (!ok || vt.range(2, 0) != 0 || vt.range(31, 8) != 0) {
                        vtype = 0x80000000;
                        vl = 0;
                    } else {
                        const ap_uint<32> vlmax = (vsew == 1) ? C::VLMAX16 : C::VLMAX32;
                        ap_uint<32> avl;
                        if (isi) avl = rs1;
                        else if (rs1 != 0) avl = a;
                        else if (rd != 0) avl = vlmax;
                        else avl = vl;
                        vtype = vt;
                        vl = (avl < vlmax) ? avl : vlmax;
                    }
                    we = true;
                    val = vl;
                } break;
                case SK_WFQ: {
                    const ap_uint<12> q = w.range(31, 20);
                    const bool push = f3[0];
                    if (q >= C::NWIN || C::win_in(q) == push) {
                        status = SC_ILLEGAL;
                        finish = true;
                    } else {
                        qv.op = push ? SC_QPUSH : SC_QPOP;
                        qv.w16 = w16cur;
                        qv.vl = vl;
                        qv.x = q;
                        qv.tx = tx;
                        qv.rmask = push ? C::win_mask(q) : ap_uint<32>(0);
                        qsend = true;
                    }
                } break;
                default: {  // SK_VEC, SK_VMEM
                    const ap_uint<5> vs1 = w.range(19, 15), vs2 = w.range(24, 20);
                    const bool vm = w[25] || vop == SC_MERGE;
                    const bool vmem = (kind == SK_VMEM);
                    const bool w16 = vmem ? (eew == 16) : w16cur;
                    const ap_uint<32> eb = vmem ? ap_uint<32>(eew >> 3) : ap_uint<32>(0);
                    const ap_uint<32> span = ap_uint<32>(vl) * eb;
                    if (vill) {
                        status = SC_ILLEGAL;
                        finish = true;
                    } else if (p_cplx && w16cur) {
                        status = SC_ILLEGAL;
                        finish = true;
                    } else if (vmem && eew == 32 && w16cur) {
                        status = SC_ILLEGAL;  // EEW > SEW needs EMUL > 1
                        finish = true;
                    } else if (vmem && (a & (4 * C::L - 1)) != 0) {
                        status = SC_ADDR;  // vle / vse base: a multiple of 4 L (D13)
                        finish = true;
                    } else if (vmem && vl != 0 && (a > C::DMB || span > C::DMB - a)) {
                        status = SC_ADDR;
                        finish = true;
                    } else {
                        sc_vi& v = qv;
                        v.op = vop;
                        v.vd = vdf;
                        v.vs1 = vs1;
                        // a store reads its data (the vd field) on the vs2 port
                        v.vs2 = (vop == SC_VSE) ? vdf : vs2;
                        v.vm = vm;
                        v.w16 = w16;
                        v.wr = p_wr;
                        v.bx = (form == F_VX || form == F_VI || form == F_VU);
                        v.vl = vl;
                        ap_uint<32> x = 0;
                        switch (form) {
                        case F_VX: x = a; break;  // rs1 is the vs1 field
                        case F_VI: { ap_int<5> s5 = vs1; x = ap_int<32>(s5); } break;
                        case F_VU: x = vs1; break;
                        default: x = 0; break;
                        }
                        if (vmem) x = a >> (2 + C::LOG2L);  // the DM bank row
                        v.x = x;
                        v.vxrm = vxrm;
                        v.csh = wfsh.range(4, 0);
                        v.rsh = wfsh.range(12, 8);
                        v.tx = tx;
                        ap_uint<32> m = 0;
                        if (p_rd_vs2 || vop == SC_VSE) m |= ap_uint<32>(1) << v.vs2;
                        if (p_rd_vs1) m |= ap_uint<32>(1) << vs1;
                        if (p_wr) m |= ap_uint<32>(1) << vdf;  // tails / masked-off keep vd
                        if (!w[25] || vop == SC_MERGE) m |= 1;
                        v.rmask = m;
                        qsend = true;
                        if (p_reply) {
                            wait = true;
                            wrd = rd;
                            wkind = 0;
                        }
                    }
                } break;
                }
            }
            if (!finish) {
                if (isdiv) {
                    phase = SCP_DIV;
                } else if (wait) {
                    phase = SCP_WAIT;
                } else {
                    if (we && rd != 0) { xwe = true; xwa = rd; xwd = val; }
                    pc = npc;
                }
            }
        } break;
        default: break;  // SCP_HALT
        }
        if (xwe) {
            xr[xwa] = xwd;
            xv[xwa] = 1;
        }
        if (qsend) vq.write(sc_pack(qv));
        if (finish) {
            ap_uint<64> sw = 0;
            sw.range(15, 0) = tx;
            sw.range(23, 16) = status;
            sw.range(63, 32) = steps;
            streamutils::write_boundary_word<ST_T, 64>(m_status, sw, true);
            phase = (status == SC_OK) ? SCP_HDR : SCP_HALT;
        }
#ifndef SC_STEP
    }
#endif
}

#endif  // WF_SIMDCORE_CORE_H
