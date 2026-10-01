// cg_vec_task.h -- the CG vector unit: register steps 2-9 of examples/mimo_cg/mimo_cg_fixed.py
// (vec_step) and the start (cg_init), bit-exact by construction from cpp/cg_ref.h.
//
// One firing is one job, driven by the command queue cmd_in (CgIterCmd):
//   INIT  read B from b_blk; X = 0, R = q_R(B), P = q_P(R), rz = q_rz(sum |R|^2); emit P on p_blk
//   ITER  read S from s_blk; one iteration; emit P on p_blk
//   LAST  read S from s_blk; one iteration; emit X on x_blk (the job ends)
// Per iteration and per column group (L columns in parallel), three pipelined passes over the K rows:
//   1. ps = q_ps(sum Re(conj(P) S))                       exact accumulator dot_ps_t
//      alpha = q_alpha(w(rz) / ps), 0 if ps == 0          L dividers
//   2. X = q_X(X + P alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
//      beta = q_beta(w(rz') / rz), 0 if rz == 0; rz = rz'
//   3. P = q_P(R + P beta)
// Every right-hand side is exact (ap_fixed operators grow their result type) and every accumulator is
// declared at its exact format, so the only rounding is the assignment to a register: the golden's
// quantize points.  Requires nit <= K (the loop bound) and N % L == 0.
// Types come from the generated cg_types.h (namespace cg).  Python twin: CgVec.run_iter (hw/vec.py).
#ifndef MIMO_CG_VEC_TASK_H
#define MIMO_CG_VEC_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_iter_cmd.h"
#include "cg_types.h"
#include "cg_lanes.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_vec_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_in,
    hls::stream_of_blocks<typename cg_lanes::group<cg::b_t, L>::type[K * N / L], SD>& b_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::s_t, L>::type[K * N / L], SD>& s_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::p_t, L>::type[K * N / L], SD>& p_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::x_t, L>::type[K * N / L], SD>& x_blk) {
    using namespace cg;
    typedef typename cg_lanes::group<b_t, L>::type b_grp;
    typedef typename cg_lanes::group<s_t, L>::type s_grp;
    typedef typename cg_lanes::group<p_t, L>::type p_grp;
    typedef typename cg_lanes::group<x_t, L>::type x_grp;
    const int NG = N / L;

    // The job's state; lanes are partitioned so all L columns of a group are touched per cycle.
    x_t xr[K][NG][L], xi[K][NG][L];
    r_t rr[K][NG][L], ri[K][NG][L];
    p_t pr[K][NG][L], pi[K][NG][L];
    rz_t rz[NG][L];
#pragma HLS ARRAY_PARTITION variable=xr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=xi dim=3 complete
#pragma HLS ARRAY_PARTITION variable=rr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=ri dim=3 complete
#pragma HLS ARRAY_PARTITION variable=pr dim=3 complete
#pragma HLS ARRAY_PARTITION variable=pi dim=3 complete
#pragma HLS ARRAY_PARTITION variable=rz dim=2 complete

    CgIterCmd cmd;
    streamutils::tlast_status tl;
    cmd.read_framed_stream<MEM_DW>(cmd_in, tl);  // INIT

    // INIT: X = 0, R = q_R(B), P = q_P(R), rz = q_rz(sum_k |R|^2)
    {
        hls::read_lock<b_grp[K * N / L]> b(b_blk);
    INIT_G:
        for (int g = 0; g < NG; ++g) {
            dot_rz_t acc[L];
#pragma HLS ARRAY_PARTITION variable=acc complete
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                acc[l] = 0;
            }
        INIT_K:
            for (int k = 0; k < K; ++k) {
#pragma HLS PIPELINE II=1
                b_t br[L], bi[L];
                cg_lanes::unpack<b_t, L>(b[k * NG + g], br, bi);
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    xr[k][g][l] = 0;
                    xi[k][g][l] = 0;
                    rr[k][g][l] = br[l];
                    ri[k][g][l] = bi[l];
                    pr[k][g][l] = rr[k][g][l];
                    pi[k][g][l] = ri[k][g][l];
                    acc[l] += rr[k][g][l] * rr[k][g][l] + ri[k][g][l] * ri[k][g][l];
                }
            }
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                rz[g][l] = acc[l];
            }
        }
    }
    {
        hls::write_lock<p_grp[K * N / L]> p(p_blk);
    INIT_P:
        for (int i = 0; i < K * NG; ++i) {
#pragma HLS PIPELINE II=1
            p[i] = cg_lanes::pack<p_t, L>(pr[i / NG][i % NG], pi[i / NG][i % NG]);
        }
    }

ITERS:
    for (int it = 0; it < K; ++it) {
        cmd.read_framed_stream<MEM_DW>(cmd_in, tl);
        const bool last = cmd.op == IterOp::LAST;
        {
            hls::read_lock<s_grp[K * N / L]> s(s_blk);
        GROUPS:
            for (int g = 0; g < NG; ++g) {
                // pass 1: ps = q_ps(sum Re(conj(P) S)), then alpha with the zero guard
                dot_ps_t ps_acc[L];
#pragma HLS ARRAY_PARTITION variable=ps_acc complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    ps_acc[l] = 0;
                }
            PASS1:
                for (int k = 0; k < K; ++k) {
#pragma HLS PIPELINE II=1
                    s_t sr[L], si[L];
                    cg_lanes::unpack<s_t, L>(s[k * NG + g], sr, si);
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        ps_acc[l] += pr[k][g][l] * sr[l] + pi[k][g][l] * si[l];
                    }
                }
                alpha_t alpha[L];
#pragma HLS ARRAY_PARTITION variable=alpha complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    ps_t ps = ps_acc[l];
                    rzw_t num = rz[g][l];
                    alpha[l] = (ps == 0) ? alpha_t(0) : alpha_t(num / ps);
                }
                // pass 2: X = q_X(X + P alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
                dot_rz_t rz_acc[L];
#pragma HLS ARRAY_PARTITION variable=rz_acc complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    rz_acc[l] = 0;
                }
            PASS2:
                for (int k = 0; k < K; ++k) {
#pragma HLS PIPELINE II=1
                    s_t sr[L], si[L];
                    cg_lanes::unpack<s_t, L>(s[k * NG + g], sr, si);
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        xr[k][g][l] = xr[k][g][l] + pr[k][g][l] * alpha[l];
                        xi[k][g][l] = xi[k][g][l] + pi[k][g][l] * alpha[l];
                        rr[k][g][l] = rr[k][g][l] - sr[l] * alpha[l];
                        ri[k][g][l] = ri[k][g][l] - si[l] * alpha[l];
                        rz_acc[l] += rr[k][g][l] * rr[k][g][l] + ri[k][g][l] * ri[k][g][l];
                    }
                }
                beta_t beta[L];
#pragma HLS ARRAY_PARTITION variable=beta complete
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    rz_t rz_new = rz_acc[l];
                    rzw_t num = rz_new;
                    beta[l] = (rz[g][l] == 0) ? beta_t(0) : beta_t(num / rz[g][l]);
                    rz[g][l] = rz_new;
                }
                // pass 3: P = q_P(R + P beta)
            PASS3:
                for (int k = 0; k < K; ++k) {
#pragma HLS PIPELINE II=1
                    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                        pr[k][g][l] = rr[k][g][l] + pr[k][g][l] * beta[l];
                        pi[k][g][l] = ri[k][g][l] + pi[k][g][l] * beta[l];
                    }
                }
            }
        }
        if (last) {
            hls::write_lock<x_grp[K * N / L]> x(x_blk);
        OUT_X:
            for (int i = 0; i < K * NG; ++i) {
#pragma HLS PIPELINE II=1
                x[i] = cg_lanes::pack<x_t, L>(xr[i / NG][i % NG], xi[i / NG][i % NG]);
            }
            break;
        }
        {
            hls::write_lock<p_grp[K * N / L]> p(p_blk);
        OUT_P:
            for (int i = 0; i < K * NG; ++i) {
#pragma HLS PIPELINE II=1
                p[i] = cg_lanes::pack<p_t, L>(pr[i / NG][i % NG], pi[i / NG][i % NG]);
            }
        }
    }
}

#endif  // MIMO_CG_VEC_TASK_H
