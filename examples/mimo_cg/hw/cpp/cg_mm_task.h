// cg_mm_task.h -- the CG matrix multiply: register step 1 of examples/mimo_cg/mimo_cg_fixed.py
// (mm_step), S = q_S(A P), as an output-stationary systolic array of R x C complex PEs.
//
// One firing is one job, driven by the command queue cmd_in (CgIterCmd):
//   INIT        read A (K rows) from a_blk and hold it for the job
//   ITER, LAST  read P from p_blk, compute S, emit S on s_blk (LAST ends the job)
// S (K x N) is covered in (K/R) x (N/C) tiles.  In a tile, A values shift right along the PE rows and
// P values shift down the PE columns, fed with the usual skew: PE (i, j) meets A[i][k] and P[k][j] at
// step t = i + j + k, and zeros outside 0 <= k < K, so it needs no valid flag.  Each PE accumulates its
// S entry exactly (mm_ap_t, the exact sum format of K products) and the tile is quantized once to s_t:
// the golden's single rounding point.  CMUL = 4 uses re = ar pr - ai pi, im = ar pi + ai pr; CMUL = 3
// uses the Gauss form k1 = pr (ar + ai), k2 = ar (pi - pr), k3 = ai (pr + pi), re = k1 - k3,
// im = k1 + k2, which is the same exact value with three multipliers.
// Requires R | K, C | N, L | C and nit <= K.  Python twin: CgMm.run_iter (examples/mimo_cg/hw/mm.py).
#ifndef MIMO_CG_MM_TASK_H
#define MIMO_CG_MM_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_iter_cmd.h"
#include "cg_types.h"
#include "cg_lanes.h"

template <int CMUL>
static inline void cg_cmac(const cg::a_t& ar, const cg::a_t& ai, const cg::p_t& pr, const cg::p_t& pi,
                           cg::mm_ap_t& acc_re, cg::mm_ap_t& acc_im) {
#pragma HLS INLINE
    if (CMUL == 3) {
        const auto k1 = pr * (ar + ai);
        const auto k2 = ar * (pi - pr);
        const auto k3 = ai * (pr + pi);
        acc_re += k1 - k3;
        acc_im += k1 + k2;
    } else {
        acc_re += ar * pr - ai * pi;
        acc_im += ar * pi + ai * pr;
    }
}

template <int MEM_DW, int K, int N, int L, int R, int C, int CMUL, int SD>
static void cg_mm_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_in,
    hls::stream_of_blocks<typename cg_lanes::group<cg::a_t, K>::type[K], SD>& a_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::p_t, L>::type[K * N / L], SD>& p_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::s_t, L>::type[K * N / L], SD>& s_blk) {
    using namespace cg;
    typedef typename cg_lanes::group<a_t, K>::type a_row;
    typedef typename cg_lanes::group<p_t, L>::type p_grp;
    typedef typename cg_lanes::group<s_t, L>::type s_grp;
    const int NG = N / L;   // lane groups per row of P and S
    const int CG = C / L;   // lane groups per tile row

    a_t ar[K][K], ai[K][K];
#pragma HLS ARRAY_PARTITION variable=ar dim=1 complete
#pragma HLS ARRAY_PARTITION variable=ai dim=1 complete

    CgIterCmd cmd;
    streamutils::tlast_status tl;
    cmd.read_framed_stream<MEM_DW>(cmd_in, tl);  // INIT
    {
        hls::read_lock<a_row[K]> a(a_blk);
    LOAD_A:
        for (int i = 0; i < K; ++i) {
#pragma HLS PIPELINE II=1
            a_t re[K], im[K];
            cg_lanes::unpack<a_t, K>(a[i], re, im);
            for (int k = 0; k < K; ++k) {
#pragma HLS UNROLL
                ar[i][k] = re[k];
                ai[i][k] = im[k];
            }
        }
    }

ITERS:
    for (int it = 0; it < K; ++it) {
        cmd.read_framed_stream<MEM_DW>(cmd_in, tl);
        const bool last = cmd.op == IterOp::LAST;
        p_t pr[K][N], pi[K][N];
#pragma HLS ARRAY_PARTITION variable=pr dim=2 cyclic factor=C
#pragma HLS ARRAY_PARTITION variable=pi dim=2 cyclic factor=C
        {
            hls::read_lock<p_grp[K * N / L]> p(p_blk);
        LOAD_P:
            for (int g = 0; g < K * NG; ++g) {
#pragma HLS PIPELINE II=1
                p_t re[L], im[L];
                cg_lanes::unpack<p_t, L>(p[g], re, im);
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    pr[g / NG][(g % NG) * L + l] = re[l];
                    pi[g / NG][(g % NG) * L + l] = im[l];
                }
            }
        }
        {
            hls::write_lock<s_grp[K * N / L]> s(s_blk);
        TILE_R:
            for (int rt = 0; rt < K / R; ++rt) {
            TILE_C:
                for (int ct = 0; ct < N / C; ++ct) {
                    mm_ap_t acc_re[R][C], acc_im[R][C];
                    a_t a_r[R][C], a_i[R][C];
                    p_t p_r[R][C], p_i[R][C];
#pragma HLS ARRAY_PARTITION variable=acc_re complete dim=0
#pragma HLS ARRAY_PARTITION variable=acc_im complete dim=0
#pragma HLS ARRAY_PARTITION variable=a_r complete dim=0
#pragma HLS ARRAY_PARTITION variable=a_i complete dim=0
#pragma HLS ARRAY_PARTITION variable=p_r complete dim=0
#pragma HLS ARRAY_PARTITION variable=p_i complete dim=0
                    for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
                        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                            acc_re[i][j] = 0;
                            acc_im[i][j] = 0;
                            a_r[i][j] = 0;
                            a_i[i][j] = 0;
                            p_r[i][j] = 0;
                            p_i[i][j] = 0;
                        }
                    }
                SWEEP:
                    for (int t = 0; t < K + R + C - 2; ++t) {
#pragma HLS PIPELINE II=1
                        // Reverse order, so every PE reads its neighbour's value from the step before.
                        for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
                            for (int j = C - 1; j >= 0; --j) {
#pragma HLS UNROLL
                                a_t a_in_r = 0, a_in_i = 0;
                                p_t p_in_r = 0, p_in_i = 0;
                                if (j == 0) {
                                    const int k = t - i;
                                    if (k >= 0 && k < K) {
                                        a_in_r = ar[rt * R + i][k];
                                        a_in_i = ai[rt * R + i][k];
                                    }
                                } else {
                                    a_in_r = a_r[i][j - 1];
                                    a_in_i = a_i[i][j - 1];
                                }
                                if (i == 0) {
                                    const int k = t - j;
                                    if (k >= 0 && k < K) {
                                        p_in_r = pr[k][ct * C + j];
                                        p_in_i = pi[k][ct * C + j];
                                    }
                                } else {
                                    p_in_r = p_r[i - 1][j];
                                    p_in_i = p_i[i - 1][j];
                                }
                                cg_cmac<CMUL>(a_in_r, a_in_i, p_in_r, p_in_i, acc_re[i][j], acc_im[i][j]);
                                a_r[i][j] = a_in_r;
                                a_i[i][j] = a_in_i;
                                p_r[i][j] = p_in_r;
                                p_i[i][j] = p_in_i;
                            }
                        }
                    }
                OUT_S:
                    for (int i = 0; i < R; ++i) {
                        for (int q = 0; q < CG; ++q) {
#pragma HLS PIPELINE II=1
                            s_t re[L], im[L];
                            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                                re[l] = acc_re[i][q * L + l];  // q_S: the one rounding
                                im[l] = acc_im[i][q * L + l];
                            }
                            s[(rt * R + i) * NG + ct * CG + q] = cg_lanes::pack<s_t, L>(re, im);
                        }
                    }
                }
            }
        }
        if (last) break;
    }
}

#endif  // MIMO_CG_MM_TASK_H
