// ltbf_engine_task.h -- the LTBF long-term CG engine: one sector solve per firing.
//
// plans/mimo_cg/ltbf_cg_dse.md step 4.1/4.2 (§9 c).  The arithmetic is examples/ltbf_cg/src/
// ltbf_ref.h's, which Vitis C-sim proved bit-exact to examples/ltbf_cg/ltbf_fixed.py (step 2.3);
// here it is restructured for throughput, not changed:
//
//   factored:  T = q_T(H^H P)       an R x C output tile per pass, the long dimension N streamed
//              S = q_S(P + H T)      an R x C output tile per pass, the 8 n_conn columns streamed
//   explicit:  Q = q_Q(I + H H^H)    once per load of H; then S = q_S(Q P) per iteration
//   then the vector step on C column lanes: ps, alpha, X, R, rz', beta, P (cg_ref.h steps 2-9).
//
// Every product is exact in its accumulator (declared at the exact sum format for the maximum
// sizes), so the only rounding is the assignment to a register -- the same values as the model.
//
// One firing: an LtbfCmd on s_in, then (load_h) n x cols words of H, then n x m words of B, each
// one complex value as a wf_lanes group of one lane; X goes out on m_out, n x m words, after nit
// iterations.  H (and Q, and cols) are static: a job with load_h = 0 reuses the last loaded ones (its
// cols field is ignored), which is how
// a sector's UEs are solved one at a time (per-UE grouping) without reloading H.
//
// On-chip storage is banked by hand so a tile's accesses fall on fixed banks:
//   H  [R][R][NMAX/R][CMAX/R]   (i % R, c % R): R rows of one column, or one row of R columns
//   P, R, X, S [C][NMAX][MMAX/C], T [C][CMAX][MMAX/C]   (k % C): C column lanes
//   Q  [R][C][NMAX/R][NMAX/C]   explicit only (requires R == C)
// Sizes and types: ltbf_config.h, generated from LtbfFormats (examples/ltbf_cg/hw/engine.py).
#ifndef LTBF_ENGINE_TASK_H
#define LTBF_ENGINE_TASK_H
#include "hls_stream.h"
#include <ap_fixed.h>
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "ltbf_cmd.h"
#include "ltbf_config.h"

namespace ltbf {

template <class T>
static inline void cplx_from_word(const ap_uint<64>& w, T& re, T& im) {
#pragma HLS INLINE
    T r[1], i[1];
    wf_lanes::unpack<T, 1>(w.range(2 * T::width - 1, 0), r, i);
    re = r[0];
    im = i[0];
}

template <class T>
static inline ap_uint<64> cplx_to_word(const T& re, const T& im) {
#pragma HLS INLINE
    T r[1] = {re}, i[1] = {im};
    ap_uint<64> w = 0;
    w.range(2 * T::width - 1, 0) = wf_lanes::pack<T, 1>(r, i);
    return w;
}

}  // namespace ltbf

static void ltbf_engine_task(hls::stream<ap_uint<64> >& s_in, hls::stream<ap_uint<64> >& m_out) {
    typedef ltbf_types TY;
    const int R = LTBF_R, C = LTBF_C;
    const int NB = LTBF_NMAX / R, CB = LTBF_CMAX / R, MB = LTBF_MMAX / C;

    static typename TY::h_t hr[R][R][NB][CB], hi[R][R][NB][CB];
#pragma HLS ARRAY_PARTITION variable=hr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=hr dim=2 complete
#pragma HLS ARRAY_PARTITION variable=hi dim=1 complete
#pragma HLS ARRAY_PARTITION variable=hi dim=2 complete
#if LTBF_EXPLICIT
    static typename TY::q_t qr[R][C][NB][LTBF_NMAX / C], qi[R][C][NB][LTBF_NMAX / C];
#pragma HLS ARRAY_PARTITION variable=qr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=qr dim=2 complete
#pragma HLS ARRAY_PARTITION variable=qi dim=1 complete
#pragma HLS ARRAY_PARTITION variable=qi dim=2 complete
#else
    typename TY::t_t tr[C][LTBF_CMAX][MB], ti[C][LTBF_CMAX][MB];
#pragma HLS ARRAY_PARTITION variable=tr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=ti dim=1 complete
#endif
    typename TY::x_t xr[C][LTBF_NMAX][MB], xi[C][LTBF_NMAX][MB];
    typename TY::r_t rr[C][LTBF_NMAX][MB], ri[C][LTBF_NMAX][MB];
    typename TY::p_t pr[C][LTBF_NMAX][MB], pi[C][LTBF_NMAX][MB];
    typename TY::s_t sr[C][LTBF_NMAX][MB], si[C][LTBF_NMAX][MB];
#pragma HLS ARRAY_PARTITION variable=xr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=xi dim=1 complete
#pragma HLS ARRAY_PARTITION variable=rr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=ri dim=1 complete
#pragma HLS ARRAY_PARTITION variable=pr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=pi dim=1 complete
#pragma HLS ARRAY_PARTITION variable=sr dim=1 complete
#pragma HLS ARRAY_PARTITION variable=si dim=1 complete
    typename TY::rz_t rz[C][MB];
    typename TY::alpha_t alpha[C][MB];
    typename TY::beta_t beta[C][MB];
#pragma HLS ARRAY_PARTITION variable=rz dim=1 complete
#pragma HLS ARRAY_PARTITION variable=alpha dim=1 complete
#pragma HLS ARRAY_PARTITION variable=beta dim=1 complete

    // The columns of the H on chip: read with H, kept for the jobs that reuse it.
    static int cols_h = 0;

    LtbfCmd cmd;
    cmd.read_stream<64>(s_in);
    if (cmd.load_h) cols_h = cmd.cols;
    const int n = cmd.n_rx, cols = cols_h, m = cmd.m, nit = cmd.nit;
    const int nrt = n / R, nct = cols / R, nmt = m / C;

    // ---- H (and, explicit, Q) ------------------------------------------------------------------
    if (cmd.load_h) {
    LOAD_H:
        for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
            for (int c = 0; c < cols; ++c) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_CMAX
#pragma HLS PIPELINE II=1
                typename TY::h_t vr, vi;
                ltbf::cplx_from_word<typename TY::h_t>(s_in.read(), vr, vi);
                hr[i % R][c % R][i / R][c / R] = vr;
                hi[i % R][c % R][i / R][c / R] = vi;
            }
        }
#if LTBF_EXPLICIT
        // Q[i][j] = sum_c H[i][c] conj(H[j][c]), plus one on the diagonal; R x C (R == C) tiles.
    Q_ROWS:
        for (int it = 0; it < nrt; ++it) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NB
        Q_COLS:
            for (int jt = 0; jt < n / C; ++jt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NC
                typename TY::acc_hh_t ar[R][C], ai[R][C];
#pragma HLS ARRAY_PARTITION variable=ar complete dim=0
#pragma HLS ARRAY_PARTITION variable=ai complete dim=0
                for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        ar[r][j] = 0;
                        ai[r][j] = 0;
                    }
                }
            Q_K:
                for (int c = 0; c < cols; ++c) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_CMAX
#pragma HLS PIPELINE II=1
                    for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                        const typename TY::h_t ar_ = hr[r][c % R][it][c / R], ai_ = hi[r][c % R][it][c / R];
                        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                            const typename TY::h_t br_ = hr[j][c % R][jt][c / R], bi_ = hi[j][c % R][jt][c / R];
                            ar[r][j] += ar_ * br_ + ai_ * bi_;
                            ai[r][j] += ai_ * br_ - ar_ * bi_;
                        }
                    }
                }
            Q_OUT:
                for (int r = 0; r < R; ++r) {
#pragma HLS PIPELINE II=1
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        const int i = it * R + r, jj = jt * C + j;
                        qr[r][j][it][jt] = (i == jj) ? typename TY::q_t(ar[r][j] + ap_int<2>(1))
                                                     : typename TY::q_t(ar[r][j]);
                        qi[r][j][it][jt] = ai[r][j];
                    }
                }
            }
        }
#endif
    }

    // ---- B: R = q_R(B), P = q_P(R), X = 0 -------------------------------------------------------
LOAD_B:
    for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
        for (int k = 0; k < m; ++k) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MMAX
#pragma HLS PIPELINE II=1
            typename TY::b_t vr, vi;
            ltbf::cplx_from_word<typename TY::b_t>(s_in.read(), vr, vi);
            const typename TY::r_t r_re = vr, r_im = vi;
            rr[k % C][i][k / C] = r_re;
            ri[k % C][i][k / C] = r_im;
            pr[k % C][i][k / C] = r_re;
            pi[k % C][i][k / C] = r_im;
            xr[k % C][i][k / C] = 0;
            xi[k % C][i][k / C] = 0;
        }
    }
    // rz = q_rz(sum |R|^2) per column
RZ0_T:
    for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
        typename TY::dot_rz_t acc[C];
#pragma HLS ARRAY_PARTITION variable=acc complete
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            acc[j] = 0;
        }
    RZ0_K:
        for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
            for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                acc[j] += rr[j][i][mt] * rr[j][i][mt] + ri[j][i][mt] * ri[j][i][mt];
            }
        }
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            rz[j][mt] = acc[j];
        }
    }

ITER:
    for (int it_ = 0; it_ < nit; ++it_) {
#pragma HLS LOOP_TRIPCOUNT max=10
#if LTBF_EXPLICIT
        // ---- S = q_S(Q P) -----------------------------------------------------------------------
    S_ROWS:
        for (int it = 0; it < nrt; ++it) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NB
        S_COLS:
            for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
                typename TY::acc_qp_t ar[R][C], ai[R][C];
#pragma HLS ARRAY_PARTITION variable=ar complete dim=0
#pragma HLS ARRAY_PARTITION variable=ai complete dim=0
                for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        ar[r][j] = 0;
                        ai[r][j] = 0;
                    }
                }
            S_K:
                for (int k = 0; k < n; ++k) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
                    for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                        const typename TY::q_t a_r = qr[r][k % C][it][k / C], a_i = qi[r][k % C][it][k / C];
                        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                            const typename TY::p_t b_r = pr[j][k][mt], b_i = pi[j][k][mt];
                            ar[r][j] += a_r * b_r - a_i * b_i;
                            ai[r][j] += a_r * b_i + a_i * b_r;
                        }
                    }
                }
            S_OUT:
                for (int r = 0; r < R; ++r) {
#pragma HLS PIPELINE II=1
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        sr[j][it * R + r][mt] = ar[r][j];
                        si[j][it * R + r][mt] = ai[r][j];
                    }
                }
            }
        }
#else
        // ---- T = q_T(H^H P): T[c][k] = sum_i conj(H[i][c]) P[i][k] ------------------------------
    T_ROWS:
        for (int ct = 0; ct < nct; ++ct) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_CB
        T_COLS:
            for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
                typename TY::acc_hp_t ar[R][C], ai[R][C];
#pragma HLS ARRAY_PARTITION variable=ar complete dim=0
#pragma HLS ARRAY_PARTITION variable=ai complete dim=0
                for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        ar[r][j] = 0;
                        ai[r][j] = 0;
                    }
                }
            T_K:
                for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
                    for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                        const typename TY::h_t h_r = hr[i % R][r][i / R][ct], h_i = hi[i % R][r][i / R][ct];
                        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                            const typename TY::p_t b_r = pr[j][i][mt], b_i = pi[j][i][mt];
                            ar[r][j] += h_r * b_r + h_i * b_i;
                            ai[r][j] += h_r * b_i - h_i * b_r;
                        }
                    }
                }
            T_OUT:
                for (int r = 0; r < R; ++r) {
#pragma HLS PIPELINE II=1
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        tr[j][ct * R + r][mt] = ar[r][j];
                        ti[j][ct * R + r][mt] = ai[r][j];
                    }
                }
            }
        }
        // ---- S = q_S(P + H T) ------------------------------------------------------------------
    U_ROWS:
        for (int it = 0; it < nrt; ++it) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NB
        U_COLS:
            for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
                typename TY::acc_ht_t ar[R][C], ai[R][C];
#pragma HLS ARRAY_PARTITION variable=ar complete dim=0
#pragma HLS ARRAY_PARTITION variable=ai complete dim=0
                for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        ar[r][j] = 0;
                        ai[r][j] = 0;
                    }
                }
            U_K:
                for (int c = 0; c < cols; ++c) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_CMAX
#pragma HLS PIPELINE II=1
                    for (int r = 0; r < R; ++r) {
#pragma HLS UNROLL
                        const typename TY::h_t h_r = hr[r][c % R][it][c / R], h_i = hi[r][c % R][it][c / R];
                        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                            const typename TY::t_t b_r = tr[j][c][mt], b_i = ti[j][c][mt];
                            ar[r][j] += h_r * b_r - h_i * b_i;
                            ai[r][j] += h_r * b_i + h_i * b_r;
                        }
                    }
                }
            U_OUT:
                for (int r = 0; r < R; ++r) {
#pragma HLS PIPELINE II=1
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        const int i = it * R + r;
                        sr[j][i][mt] = pr[j][i][mt] + ar[r][j];
                        si[j][i][mt] = pi[j][i][mt] + ai[r][j];
                    }
                }
            }
        }
#endif
        // ---- ps, then alpha with the zero guard --------------------------------------------------
    PS_T:
        for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
            typename TY::dot_ps_t acc[C];
#pragma HLS ARRAY_PARTITION variable=acc complete
            for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                acc[j] = 0;
            }
        PS_K:
            for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
                for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                    acc[j] += pr[j][i][mt] * sr[j][i][mt] + pi[j][i][mt] * si[j][i][mt];
                }
            }
        PS_DIV:
            for (int j = 0; j < C; ++j) {
#pragma HLS PIPELINE off
                const typename TY::ps_t ps = acc[j];
                const typename TY::rzw_t num = rz[j][mt];
                alpha[j][mt] = (ps == 0) ? typename TY::alpha_t(0) : typename TY::alpha_t(num / ps);
            }
        }
        // ---- X = q_X(X + P alpha), R = q_R(R - S alpha), and rz' -------------------------------
    XR_T:
        for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
            typename TY::dot_rz_t acc[C];
#pragma HLS ARRAY_PARTITION variable=acc complete
            for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                acc[j] = 0;
            }
        XR_K:
            for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
                for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                    const typename TY::alpha_t a = alpha[j][mt];
                    xr[j][i][mt] = xr[j][i][mt] + pr[j][i][mt] * a;
                    xi[j][i][mt] = xi[j][i][mt] + pi[j][i][mt] * a;
                    const typename TY::r_t nr = rr[j][i][mt] - sr[j][i][mt] * a;
                    const typename TY::r_t ni = ri[j][i][mt] - si[j][i][mt] * a;
                    rr[j][i][mt] = nr;
                    ri[j][i][mt] = ni;
                    acc[j] += nr * nr + ni * ni;
                }
            }
            // beta = q_beta(w(rz') / rz) with the zero guard; rz = rz'
        BETA_DIV:
            for (int j = 0; j < C; ++j) {
#pragma HLS PIPELINE off
                const typename TY::rz_t rz_new = acc[j];
                const typename TY::rzw_t num = rz_new;
                beta[j][mt] = (rz[j][mt] == 0) ? typename TY::beta_t(0)
                                               : typename TY::beta_t(num / rz[j][mt]);
                rz[j][mt] = rz_new;
            }
        }
        // ---- P = q_P(R + P beta) -------------------------------------------------------------------
    P_T:
        for (int mt = 0; mt < nmt; ++mt) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MB
        P_K:
            for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
#pragma HLS PIPELINE II=1
                for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                    const typename TY::beta_t b = beta[j][mt];
                    pr[j][i][mt] = rr[j][i][mt] + pr[j][i][mt] * b;
                    pi[j][i][mt] = ri[j][i][mt] + pi[j][i][mt] * b;
                }
            }
        }
    }

    // ---- X out ------------------------------------------------------------------------------------
STORE_X:
    for (int i = 0; i < n; ++i) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_NMAX
        for (int k = 0; k < m; ++k) {
#pragma HLS LOOP_TRIPCOUNT max=LTBF_MMAX
#pragma HLS PIPELINE II=1
            m_out.write(ltbf::cplx_to_word<typename TY::x_t>(xr[k % C][i][k / C], xi[k % C][i][k / C]));
        }
    }
}

#endif  // LTBF_ENGINE_TASK_H
