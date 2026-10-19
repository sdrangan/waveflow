#include "sweep.h"

void sweep(const a_t xr[MMAX][KG][L], const a_t xi[MMAX][KG][L],
           const b_t br[KMAX][NT][C], const b_t bi[KMAX][NT][C],
           acc_t out_r[MMAX][NMAX], acc_t out_i[MMAX][NMAX], int m, int k, int n) {
#pragma HLS ARRAY_PARTITION variable = xr dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = xr dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = xi dim = 1 complete
#pragma HLS ARRAY_PARTITION variable = xi dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = br dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = bi dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = out_r dim = 2 cyclic factor = 4
#pragma HLS ARRAY_PARTITION variable = out_i dim = 2 cyclic factor = 4
    const int P = period(k);
    const int nct = n / C, T = (m / R) * nct;
    // PE state
    acc_t acc_r[R][C], acc_i[R][C], sh_r[R][C], sh_i[R][C];
    a_t ax_r[R][C], ax_i[R][C];
    b_t bx_r[R][C], bx_i[R][C];
    bool fl[R][C];
#pragma HLS ARRAY_PARTITION variable = acc_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = acc_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = sh_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = sh_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = ax_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = ax_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bx_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bx_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = fl complete dim = 0
    // edge counters: row i is at (tile qr[i], step kr[i]) once t >= i; column j likewise
    int kr[R], qr[R], rtr[R], ctr[R], kc[C], qc[C], ctc[C];
#pragma HLS ARRAY_PARTITION variable = kr complete
#pragma HLS ARRAY_PARTITION variable = qr complete
#pragma HLS ARRAY_PARTITION variable = rtr complete
#pragma HLS ARRAY_PARTITION variable = ctr complete
#pragma HLS ARRAY_PARTITION variable = kc complete
#pragma HLS ARRAY_PARTITION variable = qc complete
#pragma HLS ARRAY_PARTITION variable = ctc complete
INIT_PE:
    for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            acc_r[i][j] = 0; acc_i[i][j] = 0; sh_r[i][j] = 0; sh_i[i][j] = 0;
            ax_r[i][j] = 0; ax_i[i][j] = 0;
            bx_r[i][j] = 0; bx_i[i][j] = 0;
            fl[i][j] = false;
        }
        kr[i] = 0; qr[i] = 0; rtr[i] = 0; ctr[i] = 0;
    }
INIT_COL:
    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
        kc[j] = 0; qc[j] = 0; ctc[j] = 0;
    }
    // drain: tile dq's slot dn (0..G-1) at step (dq+1)*P + L + dn; group order by d = i + cg*L
    int dq = 0, dn = 0, drt = 0, dct = 0, dwait = P + L;
    const int steps = T * P + imax(L + G, R + C - 1) + P;
STEP:
    for (int t = 0; t < steps; ++t) {
#pragma HLS LOOP_TRIPCOUNT max = 84
#pragma HLS PIPELINE II = 1
        // 1. the drain reads shadows latched on earlier steps (before this step's latches)
        if (dwait > 0) {
            --dwait;
        } else if (dq < T) {
            // slot dn -> group (i, cg), d = i + cg*L = dn (distinct while R <= L: the prototype's case)
            const int i = dn % R, cg = dn / R;
            for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                out_r[drt * R + i][dct * C + cg * L + l] = sh_r[i][cg * L + l];
                out_i[drt * R + i][dct * C + cg * L + l] = sh_i[i][cg * L + l];
            }
            if (dn == G - 1) {
                dn = 0; ++dq; dwait = P - G;
                if (dct == nct - 1) { dct = 0; ++drt; } else { ++dct; }
            } else {
                ++dn;
            }
        }
        // 2. the PEs, last first, so each reads its neighbour's value from the step before
        for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
            for (int j = C - 1; j >= 0; --j) {
#pragma HLS UNROLL
                a_t ar = 0, ai = 0;
                b_t vr = 0, vi = 0;
                bool f = false;
                if (j == 0) {
                    if (t >= i && qr[i] <= T) {
                        f = kr[i] == 0;
                        if (qr[i] < T && kr[i] < k) {
                            ar = xr[rtr[i] * R + i][kr[i] / L][kr[i] % L];
                            ai = xi[rtr[i] * R + i][kr[i] / L][kr[i] % L];
                        }
                    }
                } else {
                    ar = ax_r[i][j - 1]; ai = ax_i[i][j - 1]; f = fl[i][j - 1];
                }
                if (i == 0) {
                    if (t >= j && qc[j] < T && kc[j] < k) {
                        vr = br[kc[j]][ctc[j]][j];
                        vi = bi[kc[j]][ctc[j]][j];
                    }
                } else {
                    vr = bx_r[i - 1][j]; vi = bx_i[i - 1][j];
                }
                const acc_t pr = ar * vr - ai * vi, pi = ar * vi + ai * vr;
                if (f) {
                    sh_r[i][j] = acc_r[i][j]; sh_i[i][j] = acc_i[i][j];
                    acc_r[i][j] = pr; acc_i[i][j] = pi;
                } else {
                    acc_r[i][j] += pr; acc_i[i][j] += pi;
                }
                ax_r[i][j] = ar; ax_i[i][j] = ai; fl[i][j] = f;
                bx_r[i][j] = vr; bx_i[i][j] = vi;
            }
        }
        // 3. advance the edge counters (tile order: row tiles outer, column tiles inner)
        for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
            if (t >= i && qr[i] <= T) {
                if (kr[i] == P - 1) {
                    kr[i] = 0; ++qr[i];
                    if (ctr[i] == nct - 1) { ctr[i] = 0; ++rtr[i]; } else { ++ctr[i]; }
                } else {
                    ++kr[i];
                }
            }
        }
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            if (t >= j && qc[j] < T) {
                if (kc[j] == P - 1) {
                    kc[j] = 0; ++qc[j];
                    if (ctc[j] == nct - 1) { ctc[j] = 0; } else { ++ctc[j]; }
                } else {
                    ++kc[j];
                }
            }
        }
    }
}
