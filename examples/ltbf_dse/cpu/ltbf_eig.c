/* ltbf_eig.c -- the A53's share of the LTBF long-term update, per UE (plans/mimo_cg/ltbf_cg_dse.md
 * step 4.8): M = sym(H^H A) (8 x 8), its eigendecomposition by cyclic complex Jacobi, and the
 * projection G = (A Phi)^H of the top r eigenvectors.
 *
 * Inputs: A and H, n x 8 complex doubles drawn from the harness PRNG (wf_kernel.h); H is a rank-`rank`
 * matrix plus a small perturbation, so the Jacobi work varies with the data the way an SRS block's
 * rank makes it vary.  Every operation is an IEEE add, multiply, divide or square root in a fixed
 * order, with contraction into fused multiply-adds off, so the Python twin (examples/ltbf_dse/
 * ltbf_eig.py) reproduces every bit.
 *
 *   ./ltbf_eig n=64 r=2 rank=1 seed=3
 */
#pragma GCC optimize("fp-contract=off")
#include <math.h>
#include "wf_kernel.h"

/* The square root, correctly rounded (IEEE 754), as Python's math.sqrt: the A53's fsqrt; the
 * harness links no libm. */
static inline double wsqrt(double x) {
#ifdef __aarch64__
    double r;
    __asm__("fsqrt %d0, %d1" : "=w"(r) : "w"(x));
    return r;
#else
    return sqrt(x);
#endif
}

#define K 8
#define NMAX 1024
#define MAX_SWEEPS 12

typedef struct { double re, im; } cplx;

static cplx A[NMAX][K], H[NMAX][K];
static cplx G[2][NMAX];
static uint64_t n_rot, n_sweeps;
static double lam_top;

static double draw(void) { return (double)(int32_t)wf_rand() / 2147483648.0; }

/* M = sym(H^H A): M[p][q] = sum_i conj(H[i][p]) A[i][q], then (M + M^H) / 2. */
static WF_NOINLINE void form_m(int n, cplx m[K][K]) {
    for (int p = 0; p < K; ++p)
        for (int q = 0; q < K; ++q) {
            double re = 0.0, im = 0.0;
            for (int i = 0; i < n; ++i) {
                re += H[i][p].re * A[i][q].re + H[i][p].im * A[i][q].im;
                im += H[i][p].re * A[i][q].im - H[i][p].im * A[i][q].re;
            }
            m[p][q].re = re;
            m[p][q].im = im;
        }
    for (int p = 0; p < K; ++p)
        for (int q = p; q < K; ++q) {
            double re = 0.5 * (m[p][q].re + m[q][p].re);
            double im = 0.5 * (m[p][q].im - m[q][p].im);
            m[p][q].re = re;
            m[p][q].im = im;
            m[q][p].re = re;
            m[q][p].im = -im;
        }
}

/* Cyclic Jacobi on the Hermitian m; v gets the eigenvectors (columns). */
static WF_NOINLINE void jacobi(cplx m[K][K], cplx v[K][K]) {
    double total = 0.0;
    for (int p = 0; p < K; ++p)
        for (int q = 0; q < K; ++q) {
            total += m[p][q].re * m[p][q].re + m[p][q].im * m[p][q].im;
            v[p][q].re = p == q ? 1.0 : 0.0;
            v[p][q].im = 0.0;
        }
    for (int sweep = 0; sweep < MAX_SWEEPS; ++sweep) {
        double off = 0.0;
        for (int p = 0; p < K; ++p)
            for (int q = p + 1; q < K; ++q) off += m[p][q].re * m[p][q].re + m[p][q].im * m[p][q].im;
        if (off <= 1e-24 * total) break;
        ++n_sweeps;
        for (int p = 0; p < K - 1; ++p)
            for (int q = p + 1; q < K; ++q) {
                double mag2 = m[p][q].re * m[p][q].re + m[p][q].im * m[p][q].im;
                if (mag2 <= 1e-30 * total) continue;
                double mag = wsqrt(mag2);
                double er = m[p][q].re / mag, ei = m[p][q].im / mag; /* e = m_pq / |m_pq| */
                double tau = (m[q][q].re - m[p][p].re) / (2.0 * mag);
                double t = 1.0 / (fabs(tau) + wsqrt(1.0 + tau * tau));
                if (tau < 0.0) t = -t;
                double c = 1.0 / wsqrt(1.0 + t * t);
                double s = t * c;
                /* J = I except J_pp = J_qq = c, J_pq = s e, J_qp = -s conj(e).  m <- m J, v <- v J. */
                for (int k = 0; k < K; ++k) {
                    cplx a = m[k][p], b = m[k][q];
                    m[k][p].re = c * a.re - s * (er * b.re + ei * b.im);
                    m[k][p].im = c * a.im - s * (er * b.im - ei * b.re);
                    m[k][q].re = s * (er * a.re - ei * a.im) + c * b.re;
                    m[k][q].im = s * (er * a.im + ei * a.re) + c * b.im;
                    a = v[k][p];
                    b = v[k][q];
                    v[k][p].re = c * a.re - s * (er * b.re + ei * b.im);
                    v[k][p].im = c * a.im - s * (er * b.im - ei * b.re);
                    v[k][q].re = s * (er * a.re - ei * a.im) + c * b.re;
                    v[k][q].im = s * (er * a.im + ei * a.re) + c * b.im;
                }
                /* m <- J^H m: rows p, q (J^H_pq = -s e, J^H_qp = s conj(e)). */
                for (int k = 0; k < K; ++k) {
                    cplx a = m[p][k], b = m[q][k];
                    m[p][k].re = c * a.re - s * (er * b.re - ei * b.im);
                    m[p][k].im = c * a.im - s * (er * b.im + ei * b.re);
                    m[q][k].re = s * (er * a.re + ei * a.im) + c * b.re;
                    m[q][k].im = s * (er * a.im - ei * a.re) + c * b.im;
                }
                ++n_rot;
            }
    }
}

/* The whole per-UE step: M, its eigendecomposition, G for the top r eigenvectors. */
static WF_NOINLINE void ltbf_eig(int n, int r) {
    cplx m[K][K], v[K][K];
    int top[2] = {0, 0};
    n_rot = 0;
    n_sweeps = 0;
    form_m(n, m);
    jacobi(m, v);
    for (int j = 0; j < r; ++j) { /* the largest eigenvalue not yet taken (ties: lowest index) */
        int best = -1;
        for (int k = 0; k < K; ++k) {
            if (j == 1 && k == top[0]) continue;
            if (best < 0 || m[k][k].re > m[best][best].re) best = k;
        }
        top[j] = best;
    }
    lam_top = m[top[0]][top[0]].re;
    for (int j = 0; j < r; ++j)
        for (int i = 0; i < n; ++i) { /* G[j][i] = conj(sum_k A[i][k] v[k][top_j]) */
            double re = 0.0, im = 0.0;
            for (int k = 0; k < K; ++k) {
                cplx a = A[i][k], b = v[k][top[j]];
                re += a.re * b.re - a.im * b.im;
                im += a.re * b.im + a.im * b.re;
            }
            G[j][i].re = re;
            G[j][i].im = -im;
        }
}

int main(int argc, char **argv) {
    int n = (int)wf_arg_u64(argc, argv, "n", 64);
    int r = (int)wf_arg_u64(argc, argv, "r", 1);
    int rank = (int)wf_arg_u64(argc, argv, "rank", 8);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    if (n < 1 || n > NMAX || r < 1 || r > 2 || rank < 1 || rank > K) return 2;
    /* H = U W (n x rank times rank x 8) + 2^-10 noise; A random */
    static cplx U[NMAX][K];
    cplx W[K][K];
    for (int i = 0; i < n; ++i)
        for (int k = 0; k < rank; ++k) {
            U[i][k].re = draw();
            U[i][k].im = draw();
        }
    for (int k = 0; k < rank; ++k)
        for (int q = 0; q < K; ++q) {
            W[k][q].re = draw();
            W[k][q].im = draw();
        }
    for (int i = 0; i < n; ++i)
        for (int q = 0; q < K; ++q) {
            double re = 0.0, im = 0.0;
            for (int k = 0; k < rank; ++k) {
                re += U[i][k].re * W[k][q].re - U[i][k].im * W[k][q].im;
                im += U[i][k].re * W[k][q].im + U[i][k].im * W[k][q].re;
            }
            H[i][q].re = re + draw() * 0.0009765625;
            H[i][q].im = im + draw() * 0.0009765625;
            A[i][q].re = draw();
            A[i][q].im = draw();
        }
    ltbf_eig(n, r); /* warm-up */
    WF_ROI_BEGIN();
    ltbf_eig(n, r);
    WF_ROI_END();
    double sum = 0.0;
    for (int j = 0; j < r; ++j)
        for (int i = 0; i < n; ++i) sum += G[j][i].re + G[j][i].im;
    uint64_t sum_bits, lam_bits;
    memcpy(&sum_bits, &sum, 8);
    memcpy(&lam_bits, &lam_top, 8);
    wf_json_begin("ltbf_eig");
    wf_json_u64("n", (uint64_t)n);
    wf_json_u64("r", (uint64_t)r);
    wf_json_u64("rank", (uint64_t)rank);
    wf_json_u64("n_rot", n_rot);
    wf_json_u64("n_sweeps", n_sweeps);
    wf_json_u64("g_sum_bits", sum_bits);
    wf_json_u64("lam_bits", lam_bits);
    wf_json_end();
    return 0;
}
