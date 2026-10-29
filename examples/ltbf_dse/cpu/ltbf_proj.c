/* ltbf_proj.c -- the A53's job per UE in the LTBF long-term update (plans/ltbf_dse/ltbf_dse_spec.md
 * §3.4, §5.6): from the systolic engine's W = Ĥα'ᴴX_b (its 8 columns, every connected UE's 8 rows)
 * and the stored X (its 8 columns): M = √8·W[own rows] (8 x 8, symmetrized), its eigendecomposition by
 * cyclic complex Jacobi (as ltbf_eig.c), G = (X Φ)ᴴ for the top r eigenvectors, the cross terms
 * ΦᴴW[j]ᴴ of every other connected UE j, R̃ = GGᴴ + Σ_j (ΦᴴW[j]ᴴ)(·)ᴴ, its Cholesky factor L (r <= 2)
 * and the whitened G' = L⁻¹G (zero when R̃ is not positive definite).
 *
 * Inputs from the harness PRNG (wf_kernel.h): X random (n x 8); W random (8·n_conn x 8) except its
 * own block, a rank-`rank` product plus a small perturbation, so the Jacobi work varies as with real
 * statistics.  Every operation is an IEEE add, multiply, divide or square root in a fixed order with
 * contraction off, so the Python twin (examples/ltbf_dse/ltbf_proj.py) reproduces every bit.
 *
 *   ./ltbf_proj n=64 r=2 conn=22 rank=1 seed=3
 */
#pragma GCC optimize("fp-contract=off")
#include <math.h>
#include "wf_kernel.h"

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
#define CMAX 64
#define MAX_SWEEPS 12

typedef struct { double re, im; } cplx;

static cplx X[NMAX][K], W[K * CMAX][K];
static cplx G[2][NMAX], GP[2][NMAX], GH[2][K * CMAX];
static uint64_t n_rot, n_sweeps;
static double lam_top;

static double draw(void) { return (double)(int32_t)wf_rand() / 2147483648.0; }

/* Cyclic Jacobi on the Hermitian m; v gets the eigenvectors (columns).  ltbf_eig.c's, unchanged. */
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
                double er = m[p][q].re / mag, ei = m[p][q].im / mag;
                double tau = (m[q][q].re - m[p][p].re) / (2.0 * mag);
                double t = 1.0 / (fabs(tau) + wsqrt(1.0 + tau * tau));
                if (tau < 0.0) t = -t;
                double c = 1.0 / wsqrt(1.0 + t * t);
                double s = t * c;
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

/* The whole per-UE job (spec §3.4). */
static WF_NOINLINE int ltbf_proj(int n, int r, int conn, int pu) {
    cplx m[K][K], v[K][K];
    int top[2] = {0, 0};
    double sq8 = wsqrt(8.0);
    n_rot = 0;
    n_sweeps = 0;
    /* M = √8·W[pu block], then (M + Mᴴ) / 2 */
    for (int p = 0; p < K; ++p)
        for (int q = 0; q < K; ++q) {
            m[p][q].re = sq8 * W[pu * K + p][q].re;
            m[p][q].im = sq8 * W[pu * K + p][q].im;
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
    /* G[j][i] = conj(sum_k X[i][k] v[k][top_j]) */
    for (int j = 0; j < r; ++j)
        for (int i = 0; i < n; ++i) {
            double re = 0.0, im = 0.0;
            for (int k = 0; k < K; ++k) {
                cplx a = X[i][k], b = v[k][top[j]];
                re += a.re * b.re - a.im * b.im;
                im += a.re * b.im + a.im * b.re;
            }
            G[j][i].re = re;
            G[j][i].im = -im;
        }
    /* GH[j][row] = sum_k conj(v[k][top_j]) conj(W[row][k]) = conj(sum_k v W) */
    for (int j = 0; j < r; ++j)
        for (int row = 0; row < K * conn; ++row) {
            double re = 0.0, im = 0.0;
            for (int k = 0; k < K; ++k) {
                cplx a = W[row][k], b = v[k][top[j]];
                re += a.re * b.re - a.im * b.im;
                im += a.re * b.im + a.im * b.re;
            }
            GH[j][row].re = re;
            GH[j][row].im = -im;
        }
    /* R̃[a][b] = sum_i G[a][i] conj(G[b][i]) + sum_{rows not pu's} GH[a][row] conj(GH[b][row]) */
    cplx rt[2][2];
    for (int a = 0; a < r; ++a)
        for (int b = 0; b < r; ++b) {
            double re = 0.0, im = 0.0;
            for (int i = 0; i < n; ++i) {
                re += G[a][i].re * G[b][i].re + G[a][i].im * G[b][i].im;
                im += G[a][i].im * G[b][i].re - G[a][i].re * G[b][i].im;
            }
            for (int row = 0; row < K * conn; ++row) {
                if (row / K == pu) continue;
                re += GH[a][row].re * GH[b][row].re + GH[a][row].im * GH[b][row].im;
                im += GH[a][row].im * GH[b][row].re - GH[a][row].re * GH[b][row].im;
            }
            rt[a][b].re = re;
            rt[a][b].im = im;
        }
    /* Cholesky R̃ = L Lᴴ (r <= 2) and G' = L⁻¹ G; not positive definite: G' = 0 */
    if (!(rt[0][0].re > 0.0)) goto degenerate;
    double l00 = wsqrt(rt[0][0].re);
    for (int i = 0; i < n; ++i) {
        GP[0][i].re = G[0][i].re / l00;
        GP[0][i].im = G[0][i].im / l00;
    }
    if (r == 2) {
        double l10r = rt[1][0].re / l00, l10i = rt[1][0].im / l00;
        double d = rt[1][1].re - (l10r * l10r + l10i * l10i);
        if (!(d > 0.0)) goto degenerate;
        double l11 = wsqrt(d);
        for (int i = 0; i < n; ++i) {
            double re = G[1][i].re - (l10r * GP[0][i].re - l10i * GP[0][i].im);
            double im = G[1][i].im - (l10r * GP[0][i].im + l10i * GP[0][i].re);
            GP[1][i].re = re / l11;
            GP[1][i].im = im / l11;
        }
    }
    return 0;
degenerate:
    for (int j = 0; j < r; ++j)
        for (int i = 0; i < n; ++i) GP[j][i].re = GP[j][i].im = 0.0;
    return 1;
}

int main(int argc, char **argv) {
    int n = (int)wf_arg_u64(argc, argv, "n", 64);
    int r = (int)wf_arg_u64(argc, argv, "r", 1);
    int conn = (int)wf_arg_u64(argc, argv, "conn", 8);
    int rank = (int)wf_arg_u64(argc, argv, "rank", 8);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    if (n < 1 || n > NMAX || r < 1 || r > 2 || conn < 1 || conn > CMAX || rank < 1 || rank > K)
        return 2;
    int pu = conn / 2;
    /* X random; W random but its own block U V (rank x 8 each, 8 x 8) + 2^-10 noise */
    for (int i = 0; i < n; ++i)
        for (int k = 0; k < K; ++k) {
            X[i][k].re = draw();
            X[i][k].im = draw();
        }
    for (int row = 0; row < K * conn; ++row)
        for (int k = 0; k < K; ++k) {
            W[row][k].re = draw();
            W[row][k].im = draw();
        }
    cplx U[K][K], V[K][K];
    for (int k = 0; k < rank; ++k)
        for (int q = 0; q < K; ++q) {
            U[k][q].re = draw();
            U[k][q].im = draw();
            V[k][q].re = draw();
            V[k][q].im = draw();
        }
    for (int p = 0; p < K; ++p)
        for (int q = 0; q < K; ++q) { /* W[pu][p][q] = sum_k conj(U[k][p]) V[k][q] + noise */
            double re = 0.0, im = 0.0;
            for (int k = 0; k < rank; ++k) {
                re += U[k][p].re * V[k][q].re + U[k][p].im * V[k][q].im;
                im += U[k][p].re * V[k][q].im - U[k][p].im * V[k][q].re;
            }
            W[pu * K + p][q].re = re + draw() * 0.0009765625;
            W[pu * K + p][q].im = im + draw() * 0.0009765625;
        }
    int deg = ltbf_proj(n, r, conn, pu); /* warm-up */
    WF_ROI_BEGIN();
    deg = ltbf_proj(n, r, conn, pu);
    WF_ROI_END();
    double sum = 0.0;
    for (int j = 0; j < r; ++j)
        for (int i = 0; i < n; ++i) sum += GP[j][i].re + GP[j][i].im;
    uint64_t sum_bits, lam_bits;
    memcpy(&sum_bits, &sum, 8);
    memcpy(&lam_bits, &lam_top, 8);
    wf_json_begin("ltbf_proj");
    wf_json_u64("n", (uint64_t)n);
    wf_json_u64("r", (uint64_t)r);
    wf_json_u64("conn", (uint64_t)conn);
    wf_json_u64("rank", (uint64_t)rank);
    wf_json_u64("n_rot", n_rot);
    wf_json_u64("n_sweeps", n_sweeps);
    wf_json_u64("degenerate", (uint64_t)deg);
    wf_json_u64("gp_sum_bits", sum_bits);
    wf_json_u64("lam_bits", lam_bits);
    wf_json_end();
    return 0;
}
