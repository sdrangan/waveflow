// vecunit_core.h -- the vector unit's generic HLS parts (plans/vecunit_new.md; Python twins in
// waveflow/vecunit/). Configuration-independent: the instruction fields and checks, a slot's decoded
// form, and the matrix moves between a framed burst and a queue block. Each configuration's task
// bodies and its formats are generated into wf_vecunit_<id>.h (waveflow/vecunit/hls.py), which
// includes this header; C there is that configuration's wf_vec_cfg_<id> struct.
#ifndef WF_VECUNIT_CORE_H
#define WF_VECUNIT_CORE_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_linalg_msg.h"
#include "wf_matrix_io.h"
#include "wf_vec_instr.h"
#include "wf_vec_job.h"
#include "wf_vec_resp.h"
#include "wf_vec_status.h"

namespace wf_vec {

// waveflow.vecunit.isa: Opcode, VOp, QOp, SOp, COp; waveflow.vecunit.config.Cap
enum { OPC_V = 0x0B, OPC_Q = 0x2B, OPC_S = 0x5B, OPC_C = 0x7B };
enum { V_MVI = 0, V_MV, V_ADD, V_SUB, V_MUL, V_MACC, V_NMSAC, V_MADD, V_MACC_S, V_NMSAC_S,
       V_MADD_S, V_DOTR, V_MUL_S, V_ADD_S, V_SUB_S, V_MVS };
enum { Q_VLSE = 0, Q_VSSE, Q_LD, Q_ST };  // 4: reserved (a window read)
enum { S_DIVZ = 0, S_MV, S_LI };
enum { C_SETVL = 0, C_QPOP, C_QPUSH, C_SETRS };
enum { CAP_MOVE = 1, CAP_MUL = 2, CAP_DOT = 4, CAP_LOAD = 8, CAP_STORE = 16 };
// A scaled instruction's own result shift: imm2 bit 4 set, the shift in bits 0-3 (isa.RS_OVERRIDE).
enum { RS_OVERRIDE = 16, RS_MASK = 15 };
// What a slot does, once decoded; where an operand comes from; the two terms of R = X +- Y.
enum { K_NONE = 0, K_ARITH, K_DOT, K_LOAD, K_STORE, K_MVS };
enum { SRC_ZERO = 0, SRC_BANK, SRC_FWD, SRC_SCALAR, SRC_QUEUE };
enum { X_ZERO = 0, X_C, X_A, X_IMM };
enum { Y_ZERO = 0, Y_P, Y_B, Y_A };

inline bool is_vector(const VecInstr& d) {
#pragma HLS INLINE
    return d.opcode == OPC_V || (d.opcode == OPC_Q && (d.funct == Q_VLSE || d.funct == Q_VSSE));
}

inline bool multiplying(int f) {
#pragma HLS INLINE
    return f == V_MUL || f == V_MACC || f == V_NMSAC || f == V_MADD;
}

// The instructions whose product is scaled by 2^-RS (waveflow.vecunit.isa.SCALED).
inline bool scaled(int f) {
#pragma HLS INLINE
    return (f >= V_MUL && f <= V_DOTR) || f == V_MUL_S;
}

// wf.sli's immediate: imm2:imm1, a signed 24-bit integer.
inline ap_int<24> simm24(const VecInstr& d) {
#pragma HLS INLINE
    ap_uint<24> u;
    u(11, 0) = ap_uint<12>(d.imm1);
    u(23, 12) = ap_uint<12>(d.imm2);
    return ap_int<24>(u);
}

inline bool in(int v, int n) {
#pragma HLS INLINE
    return v >= 0 && v < n;
}

// The status the execute task gives one instruction (waveflow.vecunit.model.instr_status).
template <class C>
int instr_status(const VecInstr& d) {
#pragma HLS INLINE
    const int f = d.funct, rd = d.rd, rs1 = d.rs1, rs2 = d.rs2, i1 = d.imm1, i2 = d.imm2;
    const int BAD_OP = wf_linalg::BAD_OP, BAD_DIMS = wf_linalg::BAD_DIMS, OK = wf_linalg::OK;
    if (d.chain && !is_vector(d)) return BAD_OP;
    if (d.opcode == OPC_V) {
        if (f > (C::SEXT ? int(V_MVS) : int(V_DOTR))) return BAD_OP;  // the extensions, if built
        if ((d.conj || d.bcast) && !multiplying(f)) return BAD_OP;
        if (scaled(f) && (i2 & RS_OVERRIDE) && (i2 & RS_MASK) > C::RSM) return BAD_DIMS;
        bool ok;
        if (f == V_MVI) ok = in(rd, C::NG);
        else if (f == V_MV) ok = in(rd, C::NG) && in(rs1, C::NG);
        else if (f == V_DOTR) ok = in(rd, C::NS) && in(rs1, C::NG) && in(rs2, C::NG);
        else if (f == V_MVS) ok = in(rd, C::NS) && in(rs1, C::NG);
        else if (f >= V_MACC_S) ok = in(rd, C::NG) && in(rs1, C::NS) && in(rs2, C::NG);
        else ok = in(rd, C::NG) && in(rs1, C::NG) && in(rs2, C::NG);
        return ok ? OK : BAD_DIMS;
    }
    if (d.conj || d.bcast) return BAD_OP;
    if (d.opcode == OPC_Q) {
        if (f == Q_VLSE)
            return in(rd, C::NG) && in(rs1, C::NQI) && in(i1, C::NGQ) && in(i2 - 1, C::NGQ) ? OK
                                                                                         : BAD_DIMS;
        if (f == Q_VSSE)
            return in(rd, C::NQO) && in(rs1, C::NG) && in(i1, C::NGQ) && in(i2 - 1, C::NGQ) ? OK
                                                                                         : BAD_DIMS;
        return BAD_OP;
    }
    if (d.opcode == OPC_S) {
        if (f == S_DIVZ) return in(rd, C::NS) && in(rs1, C::NS) && in(rs2, C::NS) ? OK : BAD_DIMS;
        if (f == S_MV) return in(rd, C::NS) && in(rs1, C::NS) ? OK : BAD_DIMS;
        if (C::SEXT && f == S_LI) {
            if (!in(rd, C::NS)) return BAD_DIMS;
            // The immediate fits the scalar's format: always at 24 bits or more (it is 24 bits; and
            // 1 << (w - 1) would overflow an int at w = 32), else its range.
            const int v = simm24(d), w = C::swd(rd);
            return (w >= 24 || (v >= -(1 << (w - 1)) && v < (1 << (w - 1)))) ? OK : BAD_DIMS;
        }
        return BAD_OP;
    }
    if (d.opcode == OPC_C) {
        if (f == C_SETVL) return i1 >= 1 && i1 <= C::VLMAX ? OK : BAD_DIMS;
        if (f == C_QPOP) return in(rs1, C::NQI) ? OK : BAD_DIMS;
        if (f == C_QPUSH) return in(rd, C::NQO) ? OK : BAD_DIMS;
        if (f == C_SETRS) return i1 <= C::RSM ? OK : BAD_DIMS;
        return BAD_OP;
    }
    return BAD_OP;
}

// The slot capability an execute vector instruction needs (waveflow.vecunit.model.cap_of).
inline int cap_of(const VecInstr& d) {
#pragma HLS INLINE
    if (d.opcode == OPC_Q) return d.funct == Q_VLSE ? CAP_LOAD : CAP_STORE;
    if (d.funct <= V_SUB) return CAP_MOVE;
    return (d.funct == V_DOTR || d.funct == V_MVS) ? CAP_DOT : CAP_MUL;
}

// What an (in-range) execute vector instruction reads and writes (waveflow.vecunit.model.operands):
// up to three groups read, one scalar read, a group or scalar written, a queue read or written.
struct Ops {
    int nv;
    ap_uint<8> v[3];
    bool sr, vw, sw, qr, qw;
    ap_uint<8> s, wg, ws, q;
};

template <class C>
Ops operands(const VecInstr& d) {
#pragma HLS INLINE
    Ops o;
    o.nv = 0;
    o.v[0] = o.v[1] = o.v[2] = 0;
    o.sr = o.vw = o.sw = o.qr = o.qw = false;
    o.s = o.wg = o.ws = o.q = 0;
    const int f = d.funct;
    if (d.opcode == OPC_Q) {
        if (f == Q_VLSE) {
            o.vw = true;
            o.wg = d.rd;
            o.qr = true;
            o.q = d.rs1;
        } else {
            o.nv = 1;
            o.v[0] = d.rs1;
            o.qw = true;
            o.q = d.rd;
        }
        return o;
    }
    if (f == V_MVI) {
        o.vw = true;
        o.wg = d.rd;
    } else if (f == V_MV) {
        o.nv = 1;
        o.v[0] = d.rs1;
        o.vw = true;
        o.wg = d.rd;
    } else if (f <= V_MUL) {
        o.nv = 2;
        o.v[0] = d.rs1;
        o.v[1] = d.rs2;
        o.vw = true;
        o.wg = d.rd;
    } else if (f <= V_MADD) {
        o.nv = 3;
        o.v[0] = d.rd;
        o.v[1] = d.rs1;
        o.v[2] = d.rs2;
        o.vw = true;
        o.wg = d.rd;
    } else if (f <= V_MADD_S) {
        o.nv = 2;
        o.v[0] = d.rd;
        o.v[1] = d.rs2;
        o.sr = true;
        o.s = d.rs1;
        o.vw = true;
        o.wg = d.rd;
    } else if (f == V_DOTR) {
        o.nv = 2;
        o.v[0] = d.rs1;
        o.v[1] = d.rs2;
        o.sw = true;
        o.ws = d.rd;
    } else if (C::SEXT && f == V_MVS) {  // element 0 into a scalar: a reduction's write
        o.nv = 1;
        o.v[0] = d.rs1;
        o.sw = true;
        o.ws = d.rd;
    } else if (C::SEXT) {  // MUL_S, ADD_S, SUB_S: vs2 with a scalar
        o.nv = 1;
        o.v[0] = d.rs2;
        o.sr = true;
        o.s = d.rs1;
        o.vw = true;
        o.wg = d.rd;
    }
    return o;
}

// The pass rules (waveflow.vecunit.model.pass_status), on the ns gathered instructions.
template <class C>
int pass_status(const VecInstr d[C::S], int ns) {
#pragma HLS INLINE
    bool bread[C::NB], bwrite[C::NB], qin[C::NQI], qout[C::NQO];
    ap_uint<8> bgroup[C::NB];
#pragma HLS ARRAY_PARTITION variable = bread complete
#pragma HLS ARRAY_PARTITION variable = bwrite complete
#pragma HLS ARRAY_PARTITION variable = bgroup complete
#pragma HLS ARRAY_PARTITION variable = qin complete
#pragma HLS ARRAY_PARTITION variable = qout complete
    for (int b = 0; b < C::NB; ++b) {
#pragma HLS UNROLL
        bread[b] = bwrite[b] = false;
        bgroup[b] = 0;
    }
    for (int q = 0; q < C::NQI; ++q) {
#pragma HLS UNROLL
        qin[q] = false;
    }
    for (int q = 0; q < C::NQO; ++q) {
#pragma HLS UNROLL
        qout[q] = false;
    }
    bool written[C::S], dot[C::S];
    ap_uint<8> wgrp[C::S], dots[C::S];
    int bad = 0;
    for (int s = 0; s < C::S; ++s) {
#pragma HLS UNROLL
        written[s] = dot[s] = false;
        wgrp[s] = dots[s] = 0;
        if (s < ns) {
            if (!(cap_of(d[s]) & C::cap(s))) bad = 1;
            const Ops o = operands<C>(d[s]);
            for (int r = 0; r < 3; ++r) {
#pragma HLS UNROLL
                if (r < o.nv) {
                    bool fwd = false;
                    for (int t = 0; t < C::S; ++t) {
#pragma HLS UNROLL
                        if (t < s && written[t] && wgrp[t] == o.v[r]) fwd = true;
                    }
                    if (!fwd) {
                        const int b = o.v[r] % C::NB;
                        if (bread[b] && bgroup[b] != o.v[r]) bad = 1;
                        bread[b] = true;
                        bgroup[b] = o.v[r];
                    }
                }
            }
            if (o.sr) {
                for (int t = 0; t < C::S; ++t) {
#pragma HLS UNROLL
                    if (t < s && dot[t] && dots[t] == o.s) bad = 1;
                }
            }
            if (o.vw) {
                const int b = o.wg % C::NB;
                if (bwrite[b]) bad = 1;
                bwrite[b] = true;
                written[s] = true;
                wgrp[s] = o.wg;
            }
            if (o.sw) {
                for (int t = 0; t < C::S; ++t) {
#pragma HLS UNROLL
                    if (t < s && dot[t] && dots[t] == o.ws) bad = 1;
                }
                dot[s] = true;
                dots[s] = o.ws;
            }
            if (o.qr) {
                if (qin[o.q]) bad = 1;
                qin[o.q] = true;
            }
            if (o.qw) {
                if (qout[o.q]) bad = 1;
                qout[o.q] = true;
            }
        }
    }
    return bad ? wf_linalg::BAD_OP : wf_linalg::OK;
}

// A slot's decoded configuration: constant over its pass. Its datapath, per lane: one complex
// multiplier P = A' * B (A' the vector a, conjugated or member 0 broadcast, or a real scalar), then
// R = X +- Y with X in {0, c, a, imm} and Y in {0, P 2^-rs, b, A'}, quantized to the destination's
// format. A reduction sums P 2^-rs over the elements; wf.vmv.s.v keeps element 0's A'.
struct Slot {
    ap_uint<3> kind;
    ap_uint<3> a_src, b_src, c_src;
    ap_uint<8> a_idx, b_idx, c_idx;  // a group (then a bank or an earlier slot), a queue or a scalar
    ap_uint<2> xsel, ysel;
    bool neg, conj, bcast;
    bool rs_own;     // the instruction's own result shift, else wf.setrs's (the decode resolves it)
    ap_uint<4> rs;
    ap_uint<8> dst;   // a group, an output queue or a scalar
    ap_uint<8> dfmt;  // the destination's format id
    ap_int<12> imm;
};

template <class C>
Slot slot_of(const VecInstr& d) {
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
    s.imm = ap_int<12>(d.imm1);
    const int f = d.funct;
    // no result shift built (RSM 0): nothing of it is decoded
    s.rs_own = C::RSM && d.opcode == OPC_V && scaled(f) && (d.imm2 & RS_OVERRIDE);
    s.rs = s.rs_own ? ap_uint<4>(d.imm2 & RS_MASK) : ap_uint<4>(0);
    if (d.opcode == OPC_Q) {
        s.ysel = Y_B;
        if (f == Q_VLSE) {  // vd = q_vd(the queue's element)
            s.kind = K_LOAD;
            s.b_src = SRC_QUEUE;
            s.b_idx = d.rs1;
        } else {  // queue rd = q_q(vs)
            s.kind = K_STORE;
            s.b_src = SRC_BANK;
            s.b_idx = d.rs1;
        }
        return s;
    }
    switch (f) {
    case V_MVI: s.xsel = X_IMM; break;
    case V_MV: s.b_src = SRC_BANK; s.b_idx = d.rs1; s.ysel = Y_B; break;
    case V_ADD:
    case V_SUB:
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.xsel = X_A; s.ysel = Y_B; s.neg = f == V_SUB;
        break;
    case V_MUL: s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.ysel = Y_P; break;
    case V_MACC:
    case V_NMSAC:  // vd = vd +- a * vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.c_src = SRC_BANK; s.xsel = X_C; s.ysel = Y_P;
        s.neg = f == V_NMSAC;
        break;
    case V_MADD:  // vd = vd * a + vs2: b = vd, c = vs2
        s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.b_idx = d.rd; s.c_src = SRC_BANK;
        s.c_idx = d.rs2; s.xsel = X_C; s.ysel = Y_P;
        break;
    case V_MACC_S:
    case V_NMSAC_S:  // vd = vd +- s * vs2
        s.a_src = SRC_SCALAR; s.b_src = SRC_BANK; s.c_src = SRC_BANK; s.xsel = X_C; s.ysel = Y_P;
        s.neg = f == V_NMSAC_S;
        break;
    case V_MADD_S:  // vd = vd * s + vs2
        s.a_src = SRC_SCALAR; s.b_src = SRC_BANK; s.b_idx = d.rd; s.c_src = SRC_BANK;
        s.c_idx = d.rs2; s.xsel = X_C; s.ysel = Y_P;
        break;
    // The scalar extensions (built only with SEXT; without it the decode answers them BAD_OP).
    case V_MUL_S:  // vd = s * vs2
        if (C::SEXT) { s.a_src = SRC_SCALAR; s.b_src = SRC_BANK; s.ysel = Y_P; }
        break;
    case V_ADD_S:
    case V_SUB_S:  // vd = vs2 +- s: the scalar is A', taken straight to the adder
        if (C::SEXT) {
            s.a_src = SRC_SCALAR; s.c_src = SRC_BANK; s.c_idx = d.rs2; s.xsel = X_C; s.ysel = Y_A;
            s.neg = f == V_SUB_S;
        }
        break;
    case V_MVS:  // sd = q_sd(Re(vs1[0])): A' = vs1, held at element 0, delivered at the run's end
        if (C::SEXT) { s.kind = K_MVS; s.a_src = SRC_BANK; s.conj = false; s.bcast = false; }
        break;
    default:  // DOTR: sd = q_sd(sum Re(conj(vs1) vs2))
        s.kind = K_DOT; s.a_src = SRC_BANK; s.b_src = SRC_BANK; s.conj = true; s.bcast = false;
        break;
    }
    return s;
}

// A slot operand naming a group: forwarded from the earlier slot of the pass that writes it, else read
// from its bank, whose row base and format the pass records (one group per bank: pass_status).
template <class C>
void resolve(ap_uint<3>& src, ap_uint<8>& idx, int s, const bool written[C::S],
             const ap_uint<8> wgrp[C::S], ap_uint<16> rbase[C::NB], ap_uint<8> rfmt[C::NB]) {
#pragma HLS INLINE
    if (src != SRC_BANK) return;
    const ap_uint<8> grp = idx;
    bool fwd = false;
    ap_uint<8> from = 0;
    for (int t = 0; t < C::S; ++t) {
#pragma HLS UNROLL
        if (t < s && written[t] && wgrp[t] == grp) {
            fwd = true;
            from = t;
        }
    }
    if (fwd) {
        src = SRC_FWD;
        idx = from;
    } else {
        const int b = grp % C::NB;
        rbase[b] = (grp / C::NB) * C::VLMAX;
        rfmt[b] = C::gfmt(grp);
        idx = b;
    }
}

// Load a k x n matrix -- row-major memory elements, one burst on s -- into a block of lane groups of
// register type T: row r's ceil(n / L) groups from r * ceil(n / L), the lanes past n zero
// (waveflow.vecunit.model.block_from_matrix). A word a cycle when L divides n and a group is whole
// words; a value a cycle otherwise (the row padding). MEM moves memory elements (generated).
template <int WBW, int L, int QD, class T, class MEM, typename WT>
void load_matrix(hls::stream<WT>& s, typename wf_lanes::group<T, L>::type blk[QD], int k, int n) {
#pragma HLS INLINE
    typedef typename wf_lanes::group<T, L>::type grp_t;
    const int LW = MEM::template lane_capacity<WBW>();
    const int VB = 2 * T::width;
    streamutils::tlast_status tl;
    typename MEM::value_type buf[LW];
#pragma HLS ARRAY_PARTITION variable = buf complete
    if (n % L == 0 && L % LW == 0) {
        const int WPG = L / LW, NW = k * n / LW;
        grp_t grp = 0;
        int q = 0, g = 0;
    WORDS:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = QD * L / LW
#pragma HLS PIPELINE II = 1
            MEM::template read<WBW>(s, buf, LW, tl);
            grp_t inw = 0;
            for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
                wf_lanes::set<T, L>(inw, L - LW + i, T(buf[i].real()), T(buf[i].imag()));
            }
            grp = wf_matrix_io::shift<grp_t::width, LW * VB>::in(grp, inw);
            if (q == WPG - 1) {
                blk[g] = grp;
                ++g;
                q = 0;
            } else {
                ++q;
            }
        }
    } else {
        const int G = (n + L - 1) / L;
        grp_t grp = 0;
        int i = 0, c = 0, row = 0;
    ELEMS:
        for (int e = 0; e < k * n; ++e) {
#pragma HLS LOOP_TRIPCOUNT max = QD * L
#pragma HLS PIPELINE II = 1
            if (i == 0) {
                const int left = k * n - e;
                MEM::template read<WBW>(s, buf, left < LW ? left : LW, tl);
            }
            wf_lanes::set<T, L>(grp, c % L, T(buf[i].real()), T(buf[i].imag()));
            if (c % L == L - 1 || c == n - 1) {
                blk[row + c / L] = grp;
                grp = 0;
            }
            i = (i == LW - 1) ? 0 : i + 1;
            if (c == n - 1) {
                c = 0;
                row += G;
            } else {
                ++c;
            }
        }
    }
}

// Store a k x n matrix from a block (waveflow.vecunit.model.matrix_from_block) as one burst of
// memory elements, the last word carrying TLAST: the mirror of load_matrix.
template <int WBW, int L, int QD, class T, class MEM, typename WT>
void store_matrix(const typename wf_lanes::group<T, L>::type blk[QD], hls::stream<WT>& s, int k,
                  int n) {
#pragma HLS INLINE
    typedef typename wf_lanes::group<T, L>::type grp_t;
    const int LW = MEM::template lane_capacity<WBW>();
    const int VB = 2 * T::width;
    typename MEM::value_type buf[LW];
#pragma HLS ARRAY_PARTITION variable = buf complete
    if (n % L == 0 && L % LW == 0) {
        const int WPG = L / LW, NW = k * n / LW;
        grp_t cur = 0;
        int q = 0, g = 0;
    WORDS:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = QD * L / LW
#pragma HLS PIPELINE II = 1
            if (q == 0) cur = blk[g];
            for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
                T re, im;
                wf_lanes::get<T, L>(cur, i, re, im);
                buf[i] = typename MEM::value_type(re, im);
            }
            MEM::template write<WBW>(buf, s, w == NW - 1, LW);
            cur = wf_matrix_io::shift<grp_t::width, LW * VB>::out(cur);
            if (q == WPG - 1) {
                q = 0;
                ++g;
            } else {
                ++q;
            }
        }
    } else {
        const int G = (n + L - 1) / L;
        int i = 0, c = 0, row = 0;
    ELEMS:
        for (int e = 0; e < k * n; ++e) {
#pragma HLS LOOP_TRIPCOUNT max = QD * L
#pragma HLS PIPELINE II = 1
            T re, im;
            wf_lanes::get<T, L>(blk[row + c / L], c % L, re, im);
            buf[i] = typename MEM::value_type(re, im);
            const bool last = e == k * n - 1;
            if (i == LW - 1 || last) MEM::template write<WBW>(buf, s, last, i + 1);
            i = (i == LW - 1) ? 0 : i + 1;
            if (c == n - 1) {
                c = 0;
                row += G;
            } else {
                ++c;
            }
        }
    }
}

}  // namespace wf_vec

#endif  // WF_VECUNIT_CORE_H
