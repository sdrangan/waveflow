// cg_vector_task.h -- the CG vector core: the start and register steps 2-9 of fixed-point conjugate
// gradient (waveflow.linalg.cg: cg_init and vec_step), multi-RHS.
//
// One firing is one job, set by one CgVectorCmd on cmd_in (nit, k, n):
//   start   read B (k x n) from b_blk; X = 0, R = q_R(B), P = q_P(R), rz = q_rz(sum |R|^2); write P
//           on p_blk
//   it = 1 .. nit
//           read S = q_S(A P) from s_blk; one iteration; write the new P on p_blk, or, after the
//           last iteration, X on x_blk
// so a job reads B and nit matrices S and writes nit matrices P (P_0 .. P_nit-1) and X.  A block
// holds a matrix as row-major lane groups of L (wf_lanes.h), the systolic core's layout.
// Per iteration and per column group (L columns side by side), three pipelined passes over the k
// rows:
//   1. ps = q_ps(sum Re(conj(P) S))                       exact accumulator dot_ps_t
//      alpha = q_alpha(w(rz) / ps), 0 if ps == 0          L dividers side by side
//   2. X = q_X(X + P alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
//      beta = q_beta(w(rz') / rz), 0 if rz == 0; rz = rz'
//   3. P = q_P(R + P beta)
// Every right-hand side is exact (ap_fixed operators grow their result type) and the accumulators
// hold a sum of KMAX terms exactly, so the only rounding is the assignment to a register: the
// model's quantize points, whatever k <= KMAX.
//
// The command is not checked here (waveflow.linalg.cg_vector.cmd_status says which are valid:
// 1 <= nit <= NITMAX, 1 <= k <= KMAX, L divides n, 1 <= n <= NMAX).  Requires L a power of two and
// L | NMAX.  Types: wf_cg_traits<FID> (waveflow.linalg.cg_vector.cg_traits).  Python twin:
// waveflow.linalg.cg_vector.CgVectorCore.
#ifndef WF_CG_VECTOR_TASK_H
#define WF_CG_VECTOR_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_fixed.h>
#include <ap_int.h>
#include "wf_cg_vector_cmd.h"
#include "wf_lanes.h"
#include "wf_linalg_traits.h"

namespace wf_cg {

// The operations of the standalone unit's messages (waveflow.linalg.cg_vector.CgOp).
enum Op { START = 1, STEP = 2 };

// Lane groups of a block of ROWS x COLS values (COLS a multiple of L).
template <int ROWS, int COLS, int L>
struct blk {
    static const int n = ROWS * COLS / L;
};

}  // namespace wf_cg

template <int KMAX, int NMAX, int NITMAX, int L, int SD, int FID>
static void cg_vector_task(
    hls::stream<ap_uint<64> >& cmd_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::b_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::s_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& s_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::p_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& p_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::x_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& x_blk) {
    typedef wf_cg_traits<FID> TR;
    typedef typename TR::b_t b_t;
    typedef typename TR::s_t s_t;
    typedef typename TR::p_t p_t;
    typedef typename TR::x_t x_t;
    typedef typename TR::r_t r_t;
    typedef typename TR::rz_t rz_t;
    typedef typename TR::ps_t ps_t;
    typedef typename TR::alpha_t alpha_t;
    typedef typename TR::beta_t beta_t;
    typedef typename TR::rzw_t rzw_t;
    typedef typename TR::dot_ps_t dot_ps_t;
    typedef typename TR::dot_rz_t dot_rz_t;
    typedef typename wf_lanes::group<b_t, L>::type b_grp;
    typedef typename wf_lanes::group<s_t, L>::type s_grp;
    typedef typename wf_lanes::group<p_t, L>::type p_grp;
    typedef typename wf_lanes::group<x_t, L>::type x_grp;
    const int BG = wf_cg::blk<KMAX, NMAX, L>::n;
    const int NGMAX = NMAX / L;

    // The job's state; lanes are partitioned so all L columns of a group are touched per cycle.
    x_t xr[KMAX][NGMAX][L], xi[KMAX][NGMAX][L];
    r_t rr[KMAX][NGMAX][L], ri[KMAX][NGMAX][L];
    p_t pr[KMAX][NGMAX][L], pi[KMAX][NGMAX][L];
    rz_t rz[NGMAX][L];
#pragma HLS ARRAY_PARTITION variable=xr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=xi dim=3 complete
#pragma HLS ARRAY_PARTITION variable=rr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=ri dim=3 complete
#pragma HLS ARRAY_PARTITION variable=pr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=pi dim=3 complete
#pragma HLS ARRAY_PARTITION variable=rz dim=2 complete

    const CgVectorCmd cmd = CgVectorCmd::unpack_from_uint(cmd_in.read());
    const int nit = cmd.nit, k = cmd.k, ng = cmd.n / L;

    // The start: X = 0, R = q_R(B), P = q_P(R), rz = q_rz(sum_k |R|^2)
    {
        hls::read_lock<b_grp[BG]> b(b_blk);
    INIT_G:
        for (int g = 0; g < ng; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=NGMAX
            dot_rz_t acc[L];
#pragma HLS ARRAY_PARTITION variable=acc complete
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                acc[l] = 0;
            }
            int idx = g;  // kk * ng + g, as a counter
        INIT_K:
            for (int kk = 0; kk < k; ++kk) {
#pragma HLS LOOP_TRIPCOUNT max=KMAX
#pragma HLS PIPELINE II=1
                b_t br[L], bi[L];
                wf_lanes::unpack<b_t, L>(b[idx], br, bi);
                idx += ng;
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    xr[kk][g][l] = 0;
                    xi[kk][g][l] = 0;
                    rr[kk][g][l] = br[l];
                    ri[kk][g][l] = bi[l];
                    pr[kk][g][l] = rr[kk][g][l];
                    pi[kk][g][l] = ri[kk][g][l];
                    acc[l] += rr[kk][g][l] * rr[kk][g][l] + ri[kk][g][l] * ri[kk][g][l];
                }
            }
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                rz[g][l] = acc[l];
            }
        }
    }
    {
        hls::write_lock<p_grp[BG]> p(p_blk);
        int kk = 0, g = 0;  // i = kk * ng + g, as counters
    INIT_P:
        for (int i = 0; i < k * ng; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=BG
#pragma HLS PIPELINE II=1
            p[i] = wf_lanes::pack<p_t, L>(pr[kk][g], pi[kk][g]);
            if (++g == ng) {
                g = 0;
                ++kk;
            }
        }
    }

ITERS:
    for (int it = 1; it <= NITMAX; ++it) {
        if (it > nit) break;
        {
            hls::read_lock<s_grp[BG]> s(s_blk);
        GROUPS:
            for (int g = 0; g < ng; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=NGMAX
                // pass 1: ps = q_ps(sum Re(conj(P) S)), then alpha with the zero guard
                dot_ps_t ps_acc[L];
#pragma HLS ARRAY_PARTITION variable=ps_acc complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    ps_acc[l] = 0;
                }
                int idx1 = g;  // kk * ng + g, as a counter
            PASS1:
                for (int kk = 0; kk < k; ++kk) {
#pragma HLS LOOP_TRIPCOUNT max=KMAX
#pragma HLS PIPELINE II=1
                    s_t sr[L], si[L];
                    wf_lanes::unpack<s_t, L>(s[idx1], sr, si);
                    idx1 += ng;
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        ps_acc[l] += pr[kk][g][l] * sr[l] + pi[kk][g][l] * si[l];
                    }
                }
                alpha_t alpha[L];
#pragma HLS ARRAY_PARTITION variable=alpha complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    // Divide unconditionally by a safe divisor, then select: the L dividers then run
                    // side by side (a guarded divide is a branch HLS serializes).  Bit-exact: when
                    // ps != 0 the divisor is ps, and when ps == 0 the quotient is discarded.
                    ps_t ps = ps_acc[l];
                    ps_t den = (ps == 0) ? ps_t(1) : ps;
                    rzw_t num = rz[g][l];
                    alpha_t q = num / den;
                    alpha[l] = (ps == 0) ? alpha_t(0) : q;
                }
                // pass 2: X = q_X(X + P alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
                dot_rz_t rz_acc[L];
#pragma HLS ARRAY_PARTITION variable=rz_acc complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    rz_acc[l] = 0;
                }
                int idx2 = g;
            PASS2:
                for (int kk = 0; kk < k; ++kk) {
#pragma HLS LOOP_TRIPCOUNT max=KMAX
#pragma HLS PIPELINE II=1
                    s_t sr[L], si[L];
                    wf_lanes::unpack<s_t, L>(s[idx2], sr, si);
                    idx2 += ng;
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        xr[kk][g][l] = xr[kk][g][l] + pr[kk][g][l] * alpha[l];
                        xi[kk][g][l] = xi[kk][g][l] + pi[kk][g][l] * alpha[l];
                        rr[kk][g][l] = rr[kk][g][l] - sr[l] * alpha[l];
                        ri[kk][g][l] = ri[kk][g][l] - si[l] * alpha[l];
                        rz_acc[l] += rr[kk][g][l] * rr[kk][g][l] + ri[kk][g][l] * ri[kk][g][l];
                    }
                }
                beta_t beta[L];
#pragma HLS ARRAY_PARTITION variable=beta complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    rz_t rz_new = rz_acc[l];
                    rz_t den = (rz[g][l] == 0) ? rz_t(1) : rz[g][l];  // as for alpha
                    rzw_t num = rz_new;
                    beta_t q = num / den;
                    beta[l] = (rz[g][l] == 0) ? beta_t(0) : q;
                    rz[g][l] = rz_new;
                }
                // pass 3: P = q_P(R + P beta)
            PASS3:
                for (int kk = 0; kk < k; ++kk) {
#pragma HLS LOOP_TRIPCOUNT max=KMAX
#pragma HLS PIPELINE II=1
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        pr[kk][g][l] = rr[kk][g][l] + pr[kk][g][l] * beta[l];
                        pi[kk][g][l] = ri[kk][g][l] + pi[kk][g][l] * beta[l];
                    }
                }
            }
        }
        if (it == nit) {
            hls::write_lock<x_grp[BG]> x(x_blk);
            int kk = 0, g = 0;
        OUT_X:
            for (int i = 0; i < k * ng; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=BG
#pragma HLS PIPELINE II=1
                x[i] = wf_lanes::pack<x_t, L>(xr[kk][g], xi[kk][g]);
                if (++g == ng) {
                    g = 0;
                    ++kk;
                }
            }
        } else {
            hls::write_lock<p_grp[BG]> p(p_blk);
            int kk = 0, g = 0;
        OUT_P:
            for (int i = 0; i < k * ng; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=BG
#pragma HLS PIPELINE II=1
                p[i] = wf_lanes::pack<p_t, L>(pr[kk][g], pi[kk][g]);
                if (++g == ng) {
                    g = 0;
                    ++kk;
                }
            }
        }
    }
}

#endif  // WF_CG_VECTOR_TASK_H
