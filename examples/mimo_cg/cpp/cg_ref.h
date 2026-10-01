// cg_ref.h -- C++ reference of the bit-exact fixed-point CG detector.
//
// Step 2.4 of plans/mimo_cg/mimo_cg_paper_sims.md.  This is the ap_fixed implementation of
// examples/mimo_cg/mimo_cg_fixed.py and must reproduce it bit for bit; the register-transfer
// order below is the one documented there:
//
//   1. S     = q_S(A @ P)                  exact products, exact sum over K
//   2. ps    = q_ps(sum_k Re(conj(P) S))   per column
//   3. alpha = q_alpha(w(rz) / ps)         ps == 0 -> alpha = 0 (only rz == 0 freezes a column)
//   4. X     = q_X(X + P alpha)
//   5. R     = q_R(R - S alpha)            or q_R(B - A @ X) when T::EXPLICIT
//   6. rz'   = q_rz(sum_k |R|^2)
//   7. beta  = q_beta(w(rz') / rz)         rz == 0 -> beta = 0
//   8. rz    = rz'
//   9. P     = q_P(R + P beta)
//
// Complex values are kept as separate re/im ap_fixed arrays on purpose: std::complex<ap_fixed>
// assigns each partial product back to the element type, which would round where the model
// keeps full precision.  Every expression on the right of a register assignment is exact
// (ap_fixed operators grow their result type), and every accumulator is declared at the exact
// sum format, so the only rounding is the quantize-on-assignment to each register.
//
// T supplies the sizes and types (generated from CgFormats by mimo_cg_conformance.py):
//   static const int K, N, NIT; static const bool EXPLICIT;
//   registers  a_t b_t p_t r_t s_t x_t ps_t rz_t alpha_t beta_t, and rzw_t (widened dividend)
//   exact accumulators  mm_ap_t (A@P), mm_ax_t (A@X), dot_ps_t (Re P^H S), dot_rz_t (|R|^2)
#pragma once
#include <ap_fixed.h>

template <class T>
void cg_ref(const typename T::a_t a_re[T::K][T::K], const typename T::a_t a_im[T::K][T::K],
            const typename T::b_t b_re[T::K][T::N], const typename T::b_t b_im[T::K][T::N],
            typename T::x_t x_re[T::NIT][T::K][T::N], typename T::x_t x_im[T::NIT][T::K][T::N],
            typename T::alpha_t alpha_out[T::NIT][T::N], typename T::beta_t beta_out[T::NIT][T::N]) {
    const int K = T::K, N = T::N;
    typename T::x_t xr[K][N], xi[K][N];
    typename T::r_t rr[K][N], ri[K][N];
    typename T::p_t pr[K][N], pi[K][N];
    typename T::s_t sr[K][N], si[K][N];
    typename T::rz_t rz[N];

    for (int k = 0; k < K; ++k)
        for (int n = 0; n < N; ++n) {
            xr[k][n] = 0;
            xi[k][n] = 0;
            rr[k][n] = b_re[k][n];
            ri[k][n] = b_im[k][n];
            pr[k][n] = rr[k][n];
            pi[k][n] = ri[k][n];
        }
    for (int n = 0; n < N; ++n) {
        typename T::dot_rz_t acc = 0;
        for (int k = 0; k < K; ++k) acc += rr[k][n] * rr[k][n] + ri[k][n] * ri[k][n];
        rz[n] = acc;
    }

    for (int it = 0; it < T::NIT; ++it) {
        typename T::alpha_t alpha[N];
        // 1. S = q_S(A @ P)
        for (int k = 0; k < K; ++k)
            for (int n = 0; n < N; ++n) {
                typename T::mm_ap_t acc_re = 0, acc_im = 0;
                for (int j = 0; j < K; ++j) {
                    acc_re += a_re[k][j] * pr[j][n] - a_im[k][j] * pi[j][n];
                    acc_im += a_re[k][j] * pi[j][n] + a_im[k][j] * pr[j][n];
                }
                sr[k][n] = acc_re;
                si[k][n] = acc_im;
            }
        // 2.-3. ps, then alpha with the zero guard
        for (int n = 0; n < N; ++n) {
            typename T::dot_ps_t acc = 0;
            for (int k = 0; k < K; ++k) acc += pr[k][n] * sr[k][n] + pi[k][n] * si[k][n];
            typename T::ps_t ps = acc;
            typename T::rzw_t num = rz[n];
            alpha[n] = (ps == 0) ? typename T::alpha_t(0) : typename T::alpha_t(num / ps);
            alpha_out[it][n] = alpha[n];
        }
        // 4. X = q_X(X + P alpha)
        for (int k = 0; k < K; ++k)
            for (int n = 0; n < N; ++n) {
                xr[k][n] = xr[k][n] + pr[k][n] * alpha[n];
                xi[k][n] = xi[k][n] + pi[k][n] * alpha[n];
            }
        // 5. R
        for (int k = 0; k < K; ++k)
            for (int n = 0; n < N; ++n) {
                if (T::EXPLICIT) {
                    typename T::mm_ax_t acc_re = 0, acc_im = 0;
                    for (int j = 0; j < K; ++j) {
                        acc_re += a_re[k][j] * xr[j][n] - a_im[k][j] * xi[j][n];
                        acc_im += a_re[k][j] * xi[j][n] + a_im[k][j] * xr[j][n];
                    }
                    rr[k][n] = b_re[k][n] - acc_re;
                    ri[k][n] = b_im[k][n] - acc_im;
                } else {
                    rr[k][n] = rr[k][n] - sr[k][n] * alpha[n];
                    ri[k][n] = ri[k][n] - si[k][n] * alpha[n];
                }
            }
        // 6.-8. rz', beta with the zero guard, rz = rz'
        typename T::beta_t beta[N];
        for (int n = 0; n < N; ++n) {
            typename T::dot_rz_t acc = 0;
            for (int k = 0; k < K; ++k) acc += rr[k][n] * rr[k][n] + ri[k][n] * ri[k][n];
            typename T::rz_t rz_new = acc;
            typename T::rzw_t num = rz_new;
            beta[n] = (rz[n] == 0) ? typename T::beta_t(0) : typename T::beta_t(num / rz[n]);
            beta_out[it][n] = beta[n];
            rz[n] = rz_new;
        }
        // 9. P = q_P(R + P beta)
        for (int k = 0; k < K; ++k)
            for (int n = 0; n < N; ++n) {
                pr[k][n] = rr[k][n] + pr[k][n] * beta[n];
                pi[k][n] = ri[k][n] + pi[k][n] * beta[n];
                x_re[it][k][n] = xr[k][n];
                x_im[it][k][n] = xi[k][n];
            }
    }
}
