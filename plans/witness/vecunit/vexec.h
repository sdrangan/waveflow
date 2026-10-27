// vexec.h -- Stage 1 witness: a prototype of the vector unit's execute task (plans/vecunit_new.md).
//
// One call is one job: a VecJob word (tx_id, ninstr), then ninstr 64-bit instructions. Scalar and
// control instructions run one at a time; a vector instruction and the ones chained to it (chain
// bit) run as ONE pass: a pipelined loop over the VL elements, every slot computing per lane, a
// later slot seeing an earlier slot's result after its quantization.
//
// Register file: NB banks per lane; group g lives in bank g % NB at rows (g / NB) * VLMAX + i. NB =
// NG is the plan's "one memory per (group, lane)" case. Formats: every vector group, queue and scalar
// has a format id; the config (CFG) gives the conversions to the union formats and the quantizers back
// (a constant shift per format, then a mux on the id). Queues here are plain arrays (witnesses 1-4);
// witness 5 puts them behind stream-of-blocks.
//
// A slot's datapath, per lane: one complex multiplier P = A' * B (A' = a vector operand, conjugated or
// broadcast from member 0, or a real per-member scalar), then R = X +- Y with X in {0, C, A, imm} and
// Y in {0, P, B}, quantized to the destination's format. A reduction accumulates Re(P) exactly.
//
// Instruction word, LSB first: opcode[6:0] funct[12:7] chain[13] conj[14] bcast[15] rd[23:16]
// rs1[31:24] rs2[39:32] imm1[51:40] imm2[63:52].
#ifndef VEXEC_H
#define VEXEC_H
#include <ap_int.h>
#include <hls_stream.h>

namespace vx {

typedef ap_uint<64> word_t;

enum { OPC_V = 0x0B, OPC_Q = 0x2B, OPC_S = 0x5B, OPC_C = 0x7B };
enum { V_MVI = 0, V_MV, V_ADD, V_SUB, V_MUL, V_MACC, V_NMSAC, V_MADD, V_MACC_S, V_NMSAC_S,
       V_MADD_S, V_DOTR };
enum { Q_VLSE = 0, Q_VSSE = 1 };
enum { S_DIVZ = 0, S_MV = 1 };
enum { C_SETVL = 0, C_QPOP = 1, C_QPUSH = 2 };
// What a slot does, once decoded.
enum { K_NONE = 0, K_ARITH, K_DOT, K_LOAD, K_STORE };
// Where an operand comes from.
enum { SRC_ZERO = 0, SRC_BANK, SRC_FWD, SRC_QUEUE, SRC_SCALAR };
// The two terms of R = X +- Y.
enum { X_ZERO = 0, X_C, X_A, X_IMM };
enum { Y_ZERO = 0, Y_P, Y_B };

struct Dec {
    ap_uint<7> opc;
    ap_uint<6> funct;
    bool chain, conj, bcast;
    ap_uint<8> rd, rs1, rs2;
    ap_uint<12> imm1, imm2;
};

inline Dec decode(word_t w) {
#pragma HLS INLINE
    Dec d;
    d.opc = w(6, 0);
    d.funct = w(12, 7);
    d.chain = w[13];
    d.conj = w[14];
    d.bcast = w[15];
    d.rd = w(23, 16);
    d.rs1 = w(31, 24);
    d.rs2 = w(39, 32);
    d.imm1 = w(51, 40);
    d.imm2 = w(63, 52);
    return d;
}

// A slot's decoded configuration: constant over its pass.
struct Slot {
    ap_uint<3> kind;                  // K_*
    ap_uint<3> a_src, b_src, c_src;   // SRC_*
    ap_uint<8> a_idx, b_idx, c_idx;   // group (then bank or earlier slot), queue or scalar
    ap_uint<2> xsel, ysel;            // X_*, Y_*
    bool neg, conj, bcast;
    ap_uint<8> dst;                   // group, output queue or scalar
    ap_uint<4> dfmt;                  // the destination's format id
    ap_uint<12> g, stride;            // queue column group and row stride, in lane groups
    ap_int<12> imm;
};

inline Slot slot_of(const Dec& d) {
#pragma HLS INLINE
    Slot s;
    s.kind = K_ARITH;
    s.a_src = SRC_ZERO;
    s.b_src = SRC_ZERO;
    s.c_src = SRC_ZERO;
    s.a_idx = d.rs1;
    s.b_idx = d.rs2;
    s.c_idx = d.rd;
    s.xsel = X_ZERO;
    s.ysel = Y_ZERO;
    s.neg = false;
    s.conj = d.conj;
    s.bcast = d.bcast;
    s.dst = d.rd;
    s.dfmt = 0;
    s.g = d.imm1;
    s.stride = d.imm2;
    s.imm = ap_int<12>(d.imm1);
    if (d.opc == OPC_Q) {
        s.ysel = Y_B;
        if (d.funct == Q_VLSE) {          // vd = q_vd(column group g of queue rs1's head block)
            s.kind = K_LOAD;
            s.b_src = SRC_QUEUE;
            s.b_idx = d.rs1;
        } else {                          // queue rd's tail block, column group g = q_q(rs1)
            s.kind = K_STORE;
            s.b_src = SRC_BANK;
            s.b_idx = d.rs1;
        }
        return s;
    }
    switch (int(d.funct)) {
    case V_MVI: s.xsel = X_IMM; break;
    case V_MV: s.b_src = SRC_BANK; s.b_idx = d.rs1; s.ysel = Y_B; break;
    case V_ADD: case V_SUB:            // vd = vs1 +- vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.xsel = X_A; s.ysel = Y_B;
        s.neg = d.funct == V_SUB; s.conj = false; s.bcast = false; break;
    case V_MUL:                        // vd = vs1 * vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.ysel = Y_P; break;
    case V_MACC: case V_NMSAC:         // vd = vd +- vs1 * vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.c_src = SRC_BANK;
        s.xsel = X_C; s.ysel = Y_P; s.neg = d.funct == V_NMSAC; break;
    case V_MADD:                       // vd = vd * vs1 + vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.b_idx = d.rd; s.c_src = SRC_BANK; s.c_idx = d.rs2;
        s.xsel = X_C; s.ysel = Y_P; break;
    case V_MACC_S: case V_NMSAC_S:     // vd = vd +- s * vs2
        s.a_src = SRC_SCALAR; s.b_src = SRC_BANK; s.c_src = SRC_BANK;
        s.xsel = X_C; s.ysel = Y_P; s.neg = d.funct == V_NMSAC_S; s.conj = false; s.bcast = false;
        break;
    case V_MADD_S:                     // vd = vd * s + vs2
        s.a_src = SRC_SCALAR; s.b_src = SRC_BANK; s.b_idx = d.rd; s.c_src = SRC_BANK; s.c_idx = d.rs2;
        s.xsel = X_C; s.ysel = Y_P; s.conj = false; s.bcast = false; break;
    case V_DOTR:                       // sd = q_sd(sum Re(conj(vs1) vs2))
        s.kind = K_DOT; s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.conj = true; s.bcast = false;
        break;
    default: s.kind = K_NONE; break;
    }
    return s;
}

template <class CFG>
struct Exec {
    static const int L = CFG::L, VLMAX = CFG::VLMAX, NG = CFG::NG, NB = CFG::NB, NS = CFG::NS;
    static const int S = CFG::S, NQI = CFG::NQI, NQO = CFG::NQO, QD = CFG::QD;
    static const int DEPTH = (NG / NB) * VLMAX;
    static const int WR = CFG::WR, WQ = CFG::WQ, WSR = CFG::WSR;
    static const int FU = CFG::FU, FS = CFG::FS, FA = CFG::FA, FP = CFG::FP;
    typedef typename CFG::vu_t vu_t;     // union vector part (frac FU)
    typedef typename CFG::su_t su_t;     // union scalar (frac FS)
    typedef typename CFG::au_t au_t;     // a multiplier's A input: a vector or a scalar (frac FA)
    typedef typename CFG::res_t res_t;   // a slot's exact result part (frac FP = FA + FU)
    typedef typename CFG::dot_t dot_t;   // an exact reduction (frac FP)
    typedef ap_uint<2 * WR> cell_t;      // a stored complex element: re low, im high
    typedef ap_uint<2 * WQ * L> lg_t;    // a queue lane group

    struct cu { vu_t re, im; };

    static cu unpack_cell(cell_t c, int f) {
#pragma HLS INLINE
        cu r;
        r.re = CFG::conv_v(ap_int<WR>(c(WR - 1, 0)), f);
        r.im = CFG::conv_v(ap_int<WR>(c(2 * WR - 1, WR)), f);
        return r;
    }
    static cell_t pack_cell(ap_int<WR> re, ap_int<WR> im) {
#pragma HLS INLINE
        cell_t c;
        c(WR - 1, 0) = ap_uint<WR>(re);
        c(2 * WR - 1, WR) = ap_uint<WR>(im);
        return c;
    }
    static cu unpack_lane(lg_t g, int l, int f) {
#pragma HLS INLINE
        cu r;
        r.re = CFG::conv_v(ap_int<WR>(ap_int<WQ>(g(2 * WQ * l + WQ - 1, 2 * WQ * l))), f);
        r.im = CFG::conv_v(ap_int<WR>(ap_int<WQ>(g(2 * WQ * l + 2 * WQ - 1, 2 * WQ * l + WQ))), f);
        return r;
    }

    static cu pick(int src, int idx, int l, const cu rd[NB][L], const cu fw[S][L],
                   const cu qd[NQI][L]) {
#pragma HLS INLINE
        cu r;
        r.re = 0;
        r.im = 0;
        if (src == SRC_BANK) {
            for (int b = 0; b < NB; ++b) {
#pragma HLS UNROLL
                if (idx == b) r = rd[b][l];
            }
        } else if (src == SRC_FWD) {
            for (int s = 0; s < S; ++s) {
#pragma HLS UNROLL
                if (idx == s) r = fw[s][l];
            }
        } else if (src == SRC_QUEUE) {
            for (int q = 0; q < NQI; ++q) {
#pragma HLS UNROLL
                if (idx == q) r = qd[q][l];
            }
        }
        return r;
    }

    // ---- the job -------------------------------------------------------------------------------
    static void run(hls::stream<word_t>& prog, const lg_t qin[NQI][QD], lg_t qout[NQO][QD],
                    hls::stream<word_t>& status) {
        cell_t rf[NB][L][DEPTH];
#pragma HLS ARRAY_PARTITION variable=rf dim=1 complete
#pragma HLS ARRAY_PARTITION variable=rf dim=2 complete
#pragma HLS BIND_STORAGE variable=rf type=ram_s2p
        ap_int<WSR> sf[L][NS];
#pragma HLS ARRAY_PARTITION variable=sf dim=1 complete

        const word_t job = prog.read();
        const ap_uint<16> tx_id = job(15, 0);
        const ap_uint<16> ninstr = job(31, 16);
        int vl = VLMAX, pc = 0;
        ap_uint<8> st = 0;
    INSTR:
        while (pc < ninstr) {
            Dec d = decode(prog.read());
            ++pc;
            if (d.opc == OPC_C) {
                if (d.funct == C_SETVL) {
                    if (d.imm1 == 0 || d.imm1 > VLMAX) { st = 2; break; }
                    vl = d.imm1;
                }
                // C_QPOP / C_QPUSH: nothing to do with plain arrays (witness 5 locks them)
            } else if (d.opc == OPC_S) {
                scalar(d, sf);
            } else {
                Slot sl[S];
                bool more = true;
            GATHER:
                for (int s = 0; s < S; ++s) {
                    if (more) {
                        if (s > 0) {
                            d = decode(prog.read());
                            ++pc;
                        }
                        sl[s] = slot_of(d);
                        more = d.chain;
                    } else {
                        sl[s].kind = K_NONE;
                        sl[s].a_src = SRC_ZERO;
                        sl[s].b_src = SRC_ZERO;
                        sl[s].c_src = SRC_ZERO;
                    }
                }
                pass(sl, vl, rf, sf, qin, qout);
            }
        }
        word_t w = 0;
        w(15, 0) = tx_id;
        w(23, 16) = st;
        status.write(w);
    }

    // A group an earlier slot of the pass writes is forwarded; any other group is read from its bank.
    static void resolve(ap_uint<3>& src, ap_uint<8>& idx, int s, const bool written[S],
                        const ap_uint<8> wgrp[S], ap_uint<8> rbase[NB], ap_uint<4> rfmt[NB]) {
#pragma HLS INLINE
        if (src != SRC_BANK) return;
        const ap_uint<8> g = idx;
        bool fwd = false;
        ap_uint<8> from = 0;
        for (int t = 0; t < S; ++t) {
            if (t < s && written[t] && wgrp[t] == g) { fwd = true; from = t; }
        }
        if (fwd) {
            src = SRC_FWD;
            idx = from;
        } else {
            const int b = g % NB;
            rbase[b] = (g / NB) * VLMAX;
            rfmt[b] = CFG::gfmt(g);
            idx = b;
        }
    }

    // ---- one pass ------------------------------------------------------------------------------
    static void pass(Slot sl[S], int vl, cell_t rf[NB][L][DEPTH], ap_int<WSR> sf[L][NS],
                     const lg_t qin[NQI][QD], lg_t qout[NQO][QD]) {
#pragma HLS INLINE off
        // Decided once, here: each bank's group and format, the bank writes, each queue's slot and
        // pointer, the scalar operands.
        ap_uint<8> rbase[NB], wbase[NB];
        ap_uint<4> rfmt[NB];
        ap_uint<2> wslot[NB];
        bool wen[NB];
        for (int b = 0; b < NB; ++b) {
#pragma HLS UNROLL
            rbase[b] = 0;
            rfmt[b] = 0;
            wen[b] = false;
            wslot[b] = 0;
            wbase[b] = 0;
        }
        ap_uint<12> qip[NQI], qis[NQI], qop[NQO], qos[NQO];
        ap_uint<4> qif[NQI];
        ap_uint<2> qoslot[NQO];
        bool qoen[NQO];
        for (int q = 0; q < NQI; ++q) {
#pragma HLS UNROLL
            qip[q] = 0;
            qis[q] = 0;
            qif[q] = CFG::qfmt_in(q);
        }
        for (int q = 0; q < NQO; ++q) {
#pragma HLS UNROLL
            qop[q] = 0;
            qos[q] = 0;
            qoslot[q] = 0;
            qoen[q] = false;
        }
        bool written[S];
        ap_uint<8> wgrp[S];
        su_t sv[S][L];
#pragma HLS ARRAY_PARTITION variable=sv complete dim=0
    RESOLVE:
        for (int s = 0; s < S; ++s) {
            Slot& x = sl[s];
            resolve(x.a_src, x.a_idx, s, written, wgrp, rbase, rfmt);
            resolve(x.b_src, x.b_idx, s, written, wgrp, rbase, rfmt);
            resolve(x.c_src, x.c_idx, s, written, wgrp, rbase, rfmt);
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                sv[s][l] = x.a_src == SRC_SCALAR ? CFG::conv_s(sf[l][x.a_idx], CFG::sfmt(x.a_idx))
                                                 : su_t(0);
            }
            written[s] = x.kind == K_ARITH || x.kind == K_LOAD;
            wgrp[s] = x.dst;
            if (written[s]) {
                const int b = x.dst % NB;
                wen[b] = true;
                wslot[b] = s;
                wbase[b] = (x.dst / NB) * VLMAX;
                x.dfmt = CFG::gfmt(x.dst);
            } else if (x.kind == K_STORE) {
                x.dfmt = CFG::qfmt_out(x.dst);
                qoen[x.dst] = true;
                qoslot[x.dst] = s;
                qop[x.dst] = x.g;
                qos[x.dst] = x.stride;
            } else if (x.kind == K_DOT) {
                x.dfmt = CFG::sfmt(x.dst);
            }
            if (x.kind == K_LOAD) {
                qip[x.b_idx] = x.g;
                qis[x.b_idx] = x.stride;
            }
        }
        dot_t acc[S][L];
#pragma HLS ARRAY_PARTITION variable=acc complete dim=0
        for (int s = 0; s < S; ++s) {
#pragma HLS UNROLL
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                acc[s][l] = 0;
            }
        }

    PASS:
        for (int i = 0; i < vl; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=VLMAX
#ifdef VX_DEP_FALSE
#pragma HLS DEPENDENCE variable=rf type=inter dependent=false
#endif
            cu rd[NB][L];
#pragma HLS ARRAY_PARTITION variable=rd complete dim=0
            for (int b = 0; b < NB; ++b) {
#pragma HLS UNROLL
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    rd[b][l] = unpack_cell(rf[b][l][rbase[b] + i], rfmt[b]);
                }
            }
            cu qd[NQI][L];                    // one read a queue: its column group at row i
#pragma HLS ARRAY_PARTITION variable=qd complete dim=0
            for (int q = 0; q < NQI; ++q) {
#pragma HLS UNROLL
                const lg_t g = qin[q][qip[q]];
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    qd[q][l] = unpack_lane(g, l, qif[q]);
                }
                qip[q] += qis[q];
            }
            cu fw[S][L];
#pragma HLS ARRAY_PARTITION variable=fw complete dim=0
            ap_int<WR> wr[S][L], wi[S][L];
#pragma HLS ARRAY_PARTITION variable=wr complete dim=0
#pragma HLS ARRAY_PARTITION variable=wi complete dim=0
            for (int s = 0; s < S; ++s) {
#pragma HLS UNROLL
                const Slot& x = sl[s];
                cu a0 = pick(x.a_src, x.a_idx, 0, rd, fw, qd);   // member 0, for a broadcast
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    const cu a = x.bcast ? a0 : pick(x.a_src, x.a_idx, l, rd, fw, qd);
                    const cu b = pick(x.b_src, x.b_idx, l, rd, fw, qd);
                    const cu c = pick(x.c_src, x.c_idx, l, rd, fw, qd);
                    // the multiplier's A input: a scalar (real) or the vector a, conjugated
                    au_t ar, ai;
                    if (x.a_src == SRC_SCALAR) {
                        ar = au_t(sv[s][l]) << (FA - FS);
                        ai = 0;
                    } else {
                        ar = au_t(a.re) << (FA - FU);
                        ai = x.conj ? au_t(-au_t(a.im) << (FA - FU)) : au_t(au_t(a.im) << (FA - FU));
                    }
                    const res_t pr = res_t(ar) * b.re - res_t(ai) * b.im;
                    const res_t pi = res_t(ar) * b.im + res_t(ai) * b.re;
                    if (x.kind == K_DOT) acc[s][l] += pr;
                    res_t xr = 0, xi = 0, yr = 0, yi = 0;
                    if (x.xsel == X_C) { xr = res_t(c.re) << (FP - FU); xi = res_t(c.im) << (FP - FU); }
                    else if (x.xsel == X_A) { xr = res_t(a.re) << (FP - FU); xi = res_t(a.im) << (FP - FU); }
                    else if (x.xsel == X_IMM) { xr = res_t(x.imm) << FP; }
                    if (x.ysel == Y_P) { yr = pr; yi = pi; }
                    else if (x.ysel == Y_B) { yr = res_t(b.re) << (FP - FU); yi = res_t(b.im) << (FP - FU); }
                    const res_t rr = x.neg ? res_t(xr - yr) : res_t(xr + yr);
                    const res_t ri = x.neg ? res_t(xi - yi) : res_t(xi + yi);
                    wr[s][l] = CFG::quant_v(rr, x.dfmt);
                    wi[s][l] = CFG::quant_v(ri, x.dfmt);
                    fw[s][l].re = CFG::conv_v(wr[s][l], x.dfmt);
                    fw[s][l].im = CFG::conv_v(wi[s][l], x.dfmt);
                }
            }
            for (int b = 0; b < NB; ++b) {     // one write a bank
#pragma HLS UNROLL
                if (wen[b]) {
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        ap_int<WR> r = 0, im = 0;
                        for (int t = 0; t < S; ++t) {
#pragma HLS UNROLL
                            if (t == wslot[b]) { r = wr[t][l]; im = wi[t][l]; }
                        }
                        rf[b][l][wbase[b] + i] = pack_cell(r, im);
                    }
                }
            }
            for (int q = 0; q < NQO; ++q) {    // one write an output queue
#pragma HLS UNROLL
                if (qoen[q]) {
                    lg_t g = 0;
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        ap_int<WR> r = 0, im = 0;
                        for (int t = 0; t < S; ++t) {
#pragma HLS UNROLL
                            if (t == qoslot[q]) { r = wr[t][l]; im = wi[t][l]; }
                        }
                        g(2 * WQ * l + WQ - 1, 2 * WQ * l) = ap_uint<WQ>(ap_int<WQ>(r));
                        g(2 * WQ * l + 2 * WQ - 1, 2 * WQ * l + WQ) = ap_uint<WQ>(ap_int<WQ>(im));
                    }
                    qout[q][qop[q]] = g;
                    qop[q] += qos[q];
                }
            }
        }
        // A reduction delivers at the end of its pass.
        for (int s = 0; s < S; ++s) {
#pragma HLS UNROLL
            if (sl[s].kind == K_DOT) {
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    sf[l][sl[s].dst] = CFG::quant_dot(acc[s][l], sl[s].dfmt);
                }
            }
        }
    }

    // ---- scalar instructions -------------------------------------------------------------------
    static void scalar(const Dec& d, ap_int<WSR> sf[L][NS]) {
#pragma HLS INLINE off
        const int fa = CFG::sfmt(d.rs1), fb = CFG::sfmt(d.rs2), fd = CFG::sfmt(d.rd);
        ap_int<WSR> out[L];
#pragma HLS ARRAY_PARTITION variable=out complete
    LANES:
        for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
            const ap_int<WSR> a = sf[l][d.rs1], b = sf[l][d.rs2];
            if (d.funct == S_DIVZ) {
                // fixputils.div on w(a) and b: the quotient keeps a's widened fraction bits
                out[l] = (b == 0) ? ap_int<WSR>(0) : CFG::div_s(a, fa, b, fb, fd);
            } else {
                out[l] = CFG::quant_s(CFG::conv_s(a, fa), fd);
            }
        }
        for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
            sf[l][d.rd] = out[l];
        }
    }
};

}  // namespace vx

#endif  // VEXEC_H
