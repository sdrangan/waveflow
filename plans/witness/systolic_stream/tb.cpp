#include <cstdio>
#include <cstdlib>
#include "sweep.h"
static long long X[MMAX][KMAX][2], B[KMAX][NMAX][2];
static a_t xr[MMAX][KG][L], xi[MMAX][KG][L];
static b_t br[KMAX][NT][C], bi[KMAX][NT][C];
static acc_t outr[MMAX][NMAX], outi[MMAX][NMAX];
int main() {
    int fails = 0;
    srand(3);
    const int shapes[][3] = {{8, 8, 32}, {4, 8, 8}, {8, 4, 16}, {4, 4, 32}, {8, 8, 8}, {4, 8, 24}};
    for (auto& s : shapes) {
        const int m = s[0], k = s[1], n = s[2];
        for (int i = 0; i < MMAX; ++i)
            for (int kk = 0; kk < KMAX; ++kk)
                for (int c = 0; c < 2; ++c) X[i][kk][c] = rand() % 4096 - 2048;
        for (int kk = 0; kk < KMAX; ++kk)
            for (int j = 0; j < NMAX; ++j)
                for (int c = 0; c < 2; ++c) B[kk][j][c] = rand() % 4096 - 2048;
        for (int i = 0; i < MMAX; ++i)
            for (int kk = 0; kk < KMAX; ++kk) {
                xr[i][kk / L][kk % L] = X[i][kk][0];
                xi[i][kk / L][kk % L] = X[i][kk][1];
            }
        for (int kk = 0; kk < KMAX; ++kk)
            for (int j = 0; j < NMAX; ++j) {
                br[kk][j / C][j % C] = B[kk][j][0];
                bi[kk][j / C][j % C] = B[kk][j][1];
            }
        for (int i = 0; i < MMAX; ++i)
            for (int j = 0; j < NMAX; ++j) { outr[i][j] = -1; outi[i][j] = -1; }
        sweep(xr, xi, br, bi, outr, outi, m, k, n);
        int bad = 0;
        for (int i = 0; i < m; ++i)
            for (int j = 0; j < n; ++j) {
                long long er = 0, ei = 0;
                for (int kk = 0; kk < k; ++kk) {
                    er += X[i][kk][0] * B[kk][j][0] - X[i][kk][1] * B[kk][j][1];
                    ei += X[i][kk][0] * B[kk][j][1] + X[i][kk][1] * B[kk][j][0];
                }
                if ((long long)outr[i][j] != er || (long long)outi[i][j] != ei) ++bad;
            }
        printf("shape %dx%dx%d: %d of %d wrong, period %d, steps %d\n", m, k, n, bad, m * n,
               period(k), (m / R) * (n / C) * period(k) + imax(L + G, R + C - 1) + period(k));
        fails += bad;
    }
    printf(fails ? "FAIL\n" : "PASS\n");
    return fails ? 1 : 0;
}
