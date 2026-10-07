// systolic_core_task.h -- the systolic matrix multiply core: C = q_c(A B) or q_c(A^H B).
//
// One firing is one job, set by one SystolicCmd on cmd_in (op, nb, m, k, n): read the m x k
// matrix X from a_blk, then for each of the nb matrices B on b_blk write one C on c_blk.  For
// MUL, X = A and C = q_c(A B).  For MUL_AH, X = A^T (transposed by whoever wrote the block, not
// conjugated) and C = q_c(A^H B) = q_c(conj(X conj(B))).  A block holds a matrix as row-major lane
// groups of L (wf_lanes.h).
//
// The array has R x C complex processing elements and is output-stationary.  C (m x n) is
// covered in (m/R) x (n/C) tiles.  In a tile, X values shift right along the rows of the array and
// B values down its columns, with the usual skew: element (i, j) meets X[i][kk] and B[kk][j] at
// step t = i + j + kk, and zeros outside 0 <= kk < k, so it needs no valid flag.  Each element
// accumulates its entry exactly (acc_t, a sum of KMAX products) and the tile is rounded once to
// c_t.  FORM 4: re = xr br - xi bi, im = xr bi + xi br.  FORM 3: the Gauss form of
// complex_utils::cmult3_parts, the same exact value with three multipliers.  For MUL_AH, conj(B)
// is taken as B enters the array (its imaginary part negated in ba_t, one bit wider, so -2^(W-1)
// is exact) and the exact imaginary sum is negated before the rounding.
//
// Loading X: a row (k values) is a multiple of L or divides L.  A lane group per cycle; each row
// bank takes the group's lanes in place, so a group of short rows is written to every row it holds
// (the lanes of the other rows are never read) and the array reads row i from lane (i k) mod L on.
//
// The command is not checked here (waveflow.linalg.systolic.cmd_status says which are valid).
// Requires L a power of two, L | C, C | NMAX and R | MMAX.  Types: wf_systolic_traits<FID>
// (waveflow.linalg.systolic.core_traits).  Python twin: waveflow.linalg.systolic.SystolicCore.
#ifndef WF_SYSTOLIC_CORE_TASK_H
#define WF_SYSTOLIC_CORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_fixed.h>
#include <ap_int.h>
#include "complex_utils.hpp"
#include "wf_lanes.h"
#include "wf_linalg_traits.h"
#include "wf_systolic_cmd.h"

namespace wf_systolic {

// waveflow.linalg.systolic.MatmulOp
enum Op { MUL = 1, MUL_AH = 2 };

// Lane groups of a block of ROWS x COLS values.
template <int ROWS, int COLS, int L>
struct blk {
    static const int n = (ROWS * COLS + L - 1) / L;
};

// One complex multiply-accumulate, exact: acc += x b.
template <int FORM, class P, class A, class B, class ACC>
static inline void cmac(const A& xr, const A& xi, const B& br, const B& bi, ACC& acc_re,
                        ACC& acc_im) {
#pragma HLS INLINE
    if (FORM == 3) {
        P re, im;
        complex_utils::cmult3_parts(xr, xi, br, bi, re, im);
        acc_re += re;
        acc_im += im;
    } else {
        acc_re += xr * br - xi * bi;
        acc_im += xr * bi + xi * br;
    }
}

}  // namespace wf_systolic

template <int MMAX, int KMAX, int NMAX, int L, int R, int C, int FORM, int SD, int FID>
static void systolic_core_task(
    hls::stream<ap_uint<64> >& cmd_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, L>::type
                              [wf_systolic::blk<MMAX, KMAX, L>::n],
                          SD>& a_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, L>::type
                              [wf_systolic::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [wf_systolic::blk<MMAX, NMAX, L>::n],
                          SD>& c_blk) {
    typedef wf_systolic_traits<FID> TR;
    typedef typename TR::a_t a_t;
    typedef typename TR::b_t b_t;
    typedef typename TR::c_t c_t;
    typedef typename TR::ba_t ba_t;  // B in the array
    typedef typename TR::p_t p_t;
    typedef typename TR::acc_t acc_t;
    typedef typename wf_lanes::group<a_t, L>::type a_grp;
    typedef typename wf_lanes::group<b_t, L>::type b_grp;
    typedef typename wf_lanes::group<c_t, L>::type c_grp;
    static_assert((L & (L - 1)) == 0, "the lane count L must be a power of two");
    static_assert(C % L == 0 && NMAX % C == 0 && MMAX % R == 0, "need L | C, C | NMAX, R | MMAX");
    static_assert(FORM == 3 || FORM == 4, "FORM is 3 or 4");
    const int AG = wf_systolic::blk<MMAX, KMAX, L>::n;
    const int BG = wf_systolic::blk<KMAX, NMAX, L>::n;
    const int CG = wf_systolic::blk<MMAX, NMAX, L>::n;
    const int KG = (KMAX + L - 1) / L;  // lane groups in a row of the A store
    const int NT = NMAX / C;            // column tiles, at most
    const int CL = C / L;               // lane groups in a row of a tile

    SystolicCmd cmd;
    cmd.read_stream<64>(cmd_in);
    const bool adj = cmd.op == wf_systolic::MUL_AH;
    const int m = cmd.m, k = cmd.k, n = cmd.n, nb = cmd.nb;
    const int ng = n / L, nct = n / C, nrt = m / R;

    // X as the array reads it: value (i, kk) is at idx = off_i + kk of row i, slot idx / L,
    // lane idx % L, with off_i = (i k) mod L (0 when L divides k).
    a_t xs_r[MMAX][KG][L], xs_i[MMAX][KG][L];
#pragma HLS ARRAY_PARTITION variable=xs_r dim=1 complete
#pragma HLS ARRAY_PARTITION variable=xs_r dim=3 complete
#pragma HLS ARRAY_PARTITION variable=xs_i dim=1 complete
#pragma HLS ARRAY_PARTITION variable=xs_i dim=3 complete
    {
        const bool shrt = k < L;  // a group holds L / k whole rows
        int span = 1;             // rows a group holds
        for (int d = 1; d <= L; d *= 2) {
#pragma HLS UNROLL
            if (shrt && d * k == L) span = d;
        }
        hls::read_lock<a_grp[AG]> a(a_blk);
        int r = 0, cg = 0;  // the (first) row of the group, and the group within the row
    LOAD_A:
        for (int g = 0; g < (m * k + L - 1) / L; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=AG
#pragma HLS PIPELINE II=1
            a_t re[L], im[L];
            wf_lanes::unpack<a_t, L>(a[g], re, im);
            for (int J = 0; J < MMAX; ++J) {
#pragma HLS UNROLL
                if (J >= r && J < r + span) {
                    for (int B = 0; B < L; ++B) {
#pragma HLS UNROLL
                        xs_r[J][cg][B] = re[B];
                        xs_i[J][cg][B] = im[B];
                    }
                }
            }
            if (shrt) {
                r += span;
            } else if ((cg + 1) * L == k) {
                cg = 0;
                ++r;
            } else {
                ++cg;
            }
        }
    }

JOB:
    for (int b = 0; b < nb; ++b) {
#pragma HLS LOOP_TRIPCOUNT max=1
        // B (k x n): value (kk, ct C + j) at [kk][ct][j], so column j of the array reads bank j.
        b_t br[KMAX][NT][C], bi[KMAX][NT][C];
#pragma HLS ARRAY_PARTITION variable=br dim=3 complete
#pragma HLS ARRAY_PARTITION variable=bi dim=3 complete
        {
            hls::read_lock<b_grp[BG]> bb(b_blk);
            int row = 0, ct = 0, jg = 0;  // jg: the group within its tile row
        LOAD_B:
            for (int g = 0; g < k * ng; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=BG
#pragma HLS PIPELINE II=1
                b_t re[L], im[L];
                wf_lanes::unpack<b_t, L>(bb[g], re, im);
                for (int j = 0; j < C; ++j) {  // bank j takes lane j % L
#pragma HLS UNROLL
                    if (j / L == jg) {
                        br[row][ct][j] = re[j % L];
                        bi[row][ct][j] = im[j % L];
                    }
                }
                if (jg == CL - 1) {
                    jg = 0;
                    if (ct == nct - 1) {
                        ct = 0;
                        ++row;
                    } else {
                        ++ct;
                    }
                } else {
                    ++jg;
                }
            }
        }
        hls::write_lock<c_grp[CG]> cc(c_blk);
        int rbase = 0;  // the first group of the tile row: rt R ng
    TILE_R:
        for (int rt = 0; rt < nrt; ++rt) {
#pragma HLS LOOP_TRIPCOUNT max=MMAX/R
            int roff[R];  // off_i of the tile's rows (see the X store)
#pragma HLS ARRAY_PARTITION variable=roff complete
            for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
                roff[i] = (((rt * R + i) & (L - 1)) * (k & (L - 1))) & (L - 1);
            }
            int cbase = rbase;  // the first group of the tile: rbase + ct CL
        TILE_C:
            for (int ct = 0; ct < nct; ++ct) {
#pragma HLS LOOP_TRIPCOUNT max=NT
                acc_t acc_re[R][C], acc_im[R][C];
                a_t a_r[R][C], a_i[R][C];
                ba_t b_r[R][C], b_i[R][C];
#pragma HLS ARRAY_PARTITION variable=acc_re complete dim=0
#pragma HLS ARRAY_PARTITION variable=acc_im complete dim=0
#pragma HLS ARRAY_PARTITION variable=a_r complete dim=0
#pragma HLS ARRAY_PARTITION variable=a_i complete dim=0
#pragma HLS ARRAY_PARTITION variable=b_r complete dim=0
#pragma HLS ARRAY_PARTITION variable=b_i complete dim=0
                for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
                    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
                        acc_re[i][j] = 0;
                        acc_im[i][j] = 0;
                        a_r[i][j] = 0;
                        a_i[i][j] = 0;
                        b_r[i][j] = 0;
                        b_i[i][j] = 0;
                    }
                }
            SWEEP:
                for (int t = 0; t < k + R + C - 2; ++t) {
#pragma HLS LOOP_TRIPCOUNT max=KMAX+R+C-2
#pragma HLS PIPELINE II=1
                    // Reverse order, so every element reads its neighbour's value from the step
                    // before.
                    for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
                        for (int j = C - 1; j >= 0; --j) {
#pragma HLS UNROLL
                            a_t a_in_r = 0, a_in_i = 0;
                            ba_t b_in_r = 0, b_in_i = 0;
                            if (j == 0) {
                                const int kk = t - i;
                                if (kk >= 0 && kk < k) {
                                    const int idx = roff[i] + kk;
                                    a_in_r = xs_r[rt * R + i][idx / L][idx % L];
                                    a_in_i = xs_i[rt * R + i][idx / L][idx % L];
                                }
                            } else {
                                a_in_r = a_r[i][j - 1];
                                a_in_i = a_i[i][j - 1];
                            }
                            if (i == 0) {
                                const int kk = t - j;
                                if (kk >= 0 && kk < k) {
                                    const ba_t v = bi[kk][ct][j];
                                    b_in_r = br[kk][ct][j];
                                    b_in_i = adj ? ba_t(-v) : v;  // conj(B) as it enters
                                }
                            } else {
                                b_in_r = b_r[i - 1][j];
                                b_in_i = b_i[i - 1][j];
                            }
                            wf_systolic::cmac<FORM, p_t>(a_in_r, a_in_i, b_in_r, b_in_i,
                                                         acc_re[i][j], acc_im[i][j]);
                            a_r[i][j] = a_in_r;
                            a_i[i][j] = a_in_i;
                            b_r[i][j] = b_in_r;
                            b_i[i][j] = b_in_i;
                        }
                    }
                }
                int obase = cbase;  // the first group of the tile's row i
            OUT_C:
                for (int i = 0; i < R; ++i) {
                    for (int q = 0; q < CL; ++q) {
#pragma HLS PIPELINE II=1
                        c_t re[L], im[L];
                        for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                            const acc_t v = acc_im[i][q * L + l];
                            re[l] = acc_re[i][q * L + l];  // q_c: the one rounding
                            im[l] = adj ? acc_t(-v) : v;   // conj before the rounding
                        }
                        cc[obase + q] = wf_lanes::pack<c_t, L>(re, im);
                    }
                    obase += ng;
                }
                cbase += CL;
            }
            rbase += R * ng;
        }
    }
}

#endif  // WF_SYSTOLIC_CORE_TASK_H
