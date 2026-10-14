// ltbf_ref.h -- C++ reference of the bit-exact fixed-point LTBF CG solve.
//
// Step 2.3 of plans/mimo_cg/ltbf_cg_dse.md.  This is the ap_fixed implementation of
// examples/ltbf_cg/ltbf_fixed.py (solve_fixed) and must reproduce it bit for bit.  It is
// examples/mimo_cg/cpp/cg_ref.h with step 1 replaced by the LTBF products and the recurrence
// residual only:
//
//   0. explicit: Q = q_Q(I + H H^H)            exact sum over the C columns, one rounding
//   1. explicit: S = q_S(Q @ P)
//      factored: T = q_T(H^H @ P), S = q_S(P + H @ T)
//   2. ps    = q_ps(sum_k Re(conj(P) S))       per column
//   3. alpha = q_alpha(w(rz) / ps)             ps == 0 -> alpha = 0
//   4. X     = q_X(X + P alpha)
//   5. R     = q_R(R - S alpha)
//   6. rz'   = q_rz(sum_k |R|^2)
//   7. beta  = q_beta(w(rz') / rz)             rz == 0 -> beta = 0
//   8. rz    = rz'
//   9. P     = q_P(R + P beta)
//
// Complex values are separate re/im ap_fixed arrays, every expression on the right of a register
// assignment is exact (ap_fixed operators grow their result type) and every accumulator is
// declared at its exact sum format, so the only rounding is the quantize-on-assignment to each
// register (see cg_ref.h).
//
// C supplies the sizes and types (generated from LtbfFormats by ltbf_conformance.py):
//   static const int N, COLS, M, NIT; static const bool EXPLICIT;
//   registers  h_t q_t t_t b_t p_t r_t s_t x_t ps_t rz_t alpha_t beta_t, rzw_t (widened dividend)
//   exact accumulators  acc_hh_t (H H^H), acc_qp_t (Q P), acc_hp_t (H^H P), acc_ht_t (H T),
//                       dot_ps_t (Re P^H S), dot_rz_t (|R|^2)
#pragma once
#include <ap_fixed.h>

template <class C>
void ltbf_ref(const typename C::h_t h_re[C::N][C::COLS], const typename C::h_t h_im[C::N][C::COLS],
              const typename C::b_t b_re[C::N][C::M], const typename C::b_t b_im[C::N][C::M],
              typename C::x_t x_re[C::NIT][C::N][C::M], typename C::x_t x_im[C::NIT][C::N][C::M],
              typename C::alpha_t alpha_out[C::NIT][C::M], typename C::beta_t beta_out[C::NIT][C::M]) {
    const int N = C::N, COLS = C::COLS, M = C::M;
    static typename C::q_t qr[C::N][C::N], qi[C::N][C::N];
    static typename C::t_t tr[C::COLS][C::M], ti[C::COLS][C::M];
    static typename C::x_t xr[C::N][C::M], xi[C::N][C::M];
    static typename C::r_t rr[C::N][C::M], ri[C::N][C::M];
    static typename C::p_t pr[C::N][C::M], pi[C::N][C::M];
    static typename C::s_t sr[C::N][C::M], si[C::N][C::M];
    typename C::rz_t rz[C::M];

    // 0. Q = q_Q(I + H H^H): Q[i][j] = sum_c H[i][c] conj(H[j][c]), plus one on the diagonal
    if (C::EXPLICIT)
        for (int i = 0; i < N; ++i)
            for (int j = 0; j < N; ++j) {
                typename C::acc_hh_t acc_re = 0, acc_im = 0;
                for (int c = 0; c < COLS; ++c) {
                    acc_re += h_re[i][c] * h_re[j][c] + h_im[i][c] * h_im[j][c];
                    acc_im += h_im[i][c] * h_re[j][c] - h_re[i][c] * h_im[j][c];
                }
                qr[i][j] = (i == j) ? typename C::q_t(acc_re + ap_int<2>(1)) : typename C::q_t(acc_re);
                qi[i][j] = acc_im;
            }

    for (int k = 0; k < N; ++k)
        for (int n = 0; n < M; ++n) {
            xr[k][n] = 0;
            xi[k][n] = 0;
            rr[k][n] = b_re[k][n];
            ri[k][n] = b_im[k][n];
            pr[k][n] = rr[k][n];
            pi[k][n] = ri[k][n];
        }
    for (int n = 0; n < M; ++n) {
        typename C::dot_rz_t acc = 0;
        for (int k = 0; k < N; ++k) acc += rr[k][n] * rr[k][n] + ri[k][n] * ri[k][n];
        rz[n] = acc;
    }

    for (int it = 0; it < C::NIT; ++it) {
        typename C::alpha_t alpha[C::M];
        // 1. S
        if (C::EXPLICIT) {
            for (int k = 0; k < N; ++k)
                for (int n = 0; n < M; ++n) {
                    typename C::acc_qp_t acc_re = 0, acc_im = 0;
                    for (int j = 0; j < N; ++j) {
                        acc_re += qr[k][j] * pr[j][n] - qi[k][j] * pi[j][n];
                        acc_im += qr[k][j] * pi[j][n] + qi[k][j] * pr[j][n];
                    }
                    sr[k][n] = acc_re;
                    si[k][n] = acc_im;
                }
        } else {
            // T = q_T(H^H P): T[c][n] = sum_i conj(H[i][c]) P[i][n]
            for (int c = 0; c < COLS; ++c)
                for (int n = 0; n < M; ++n) {
                    typename C::acc_hp_t acc_re = 0, acc_im = 0;
                    for (int i = 0; i < N; ++i) {
                        acc_re += h_re[i][c] * pr[i][n] + h_im[i][c] * pi[i][n];
                        acc_im += h_re[i][c] * pi[i][n] - h_im[i][c] * pr[i][n];
                    }
                    tr[c][n] = acc_re;
                    ti[c][n] = acc_im;
                }
            // S = q_S(P + H T)
            for (int k = 0; k < N; ++k)
                for (int n = 0; n < M; ++n) {
                    typename C::acc_ht_t acc_re = 0, acc_im = 0;
                    for (int c = 0; c < COLS; ++c) {
                        acc_re += h_re[k][c] * tr[c][n] - h_im[k][c] * ti[c][n];
                        acc_im += h_re[k][c] * ti[c][n] + h_im[k][c] * tr[c][n];
                    }
                    sr[k][n] = pr[k][n] + acc_re;
                    si[k][n] = pi[k][n] + acc_im;
                }
        }
        // 2.-3. ps, then alpha with the zero guard
        for (int n = 0; n < M; ++n) {
            typename C::dot_ps_t acc = 0;
            for (int k = 0; k < N; ++k) acc += pr[k][n] * sr[k][n] + pi[k][n] * si[k][n];
            typename C::ps_t ps = acc;
            typename C::rzw_t num = rz[n];
            alpha[n] = (ps == 0) ? typename C::alpha_t(0) : typename C::alpha_t(num / ps);
            alpha_out[it][n] = alpha[n];
        }
        // 4. X = q_X(X + P alpha); 5. R = q_R(R - S alpha)
        for (int k = 0; k < N; ++k)
            for (int n = 0; n < M; ++n) {
                xr[k][n] = xr[k][n] + pr[k][n] * alpha[n];
                xi[k][n] = xi[k][n] + pi[k][n] * alpha[n];
                rr[k][n] = rr[k][n] - sr[k][n] * alpha[n];
                ri[k][n] = ri[k][n] - si[k][n] * alpha[n];
            }
        // 6.-8. rz', beta with the zero guard, rz = rz'
        typename C::beta_t beta[C::M];
        for (int n = 0; n < M; ++n) {
            typename C::dot_rz_t acc = 0;
            for (int k = 0; k < N; ++k) acc += rr[k][n] * rr[k][n] + ri[k][n] * ri[k][n];
            typename C::rz_t rz_new = acc;
            typename C::rzw_t num = rz_new;
            beta[n] = (rz[n] == 0) ? typename C::beta_t(0) : typename C::beta_t(num / rz[n]);
            beta_out[it][n] = beta[n];
            rz[n] = rz_new;
        }
        // 9. P = q_P(R + P beta)
        for (int k = 0; k < N; ++k)
            for (int n = 0; n < M; ++n) {
                pr[k][n] = rr[k][n] + pr[k][n] * beta[n];
                pi[k][n] = ri[k][n] + pi[k][n] * beta[n];
                x_re[it][k][n] = xr[k][n];
                x_im[it][k][n] = xi[k][n];
            }
    }
}
