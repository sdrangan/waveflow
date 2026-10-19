// systolic_engine_core_task.h -- the systolic engine's core: a streaming systolic array.
//
// One firing is one job (EngineJob): per output, per panel, it holds the X block (A, or A^T for
// MUL_AH) and the B block and sweeps them in place -- no copy: the blocks are banked as the array
// reads them (X a bank per row, B a bank per array column).  keep_a is the A loader's: it repeats its
// one A per output, so the core takes an X block per panel either way (one call site of the array).
//
// The array has R x C complex processing elements, output-stationary, and runs every tile of a panel
// in ONE pipelined loop (plans/witness/systolic_stream).  Row i of the array takes tile q's value kk at
// step q P + kk + i, column j likewise at q P + kk + j, zero for kk >= kp, so element (i, j) meets
// X[i][kk] and B[kk][j] at one step; P = max(kp, R C / L, L) is the tile period (a tile shorter than
// P is padded with zero steps, which add nothing).  A "first" flag rides with the X input: when it
// reaches an element, the element latches its sum into a shadow register and starts the next tile at
// once -- from zero on the first panel, from its partial sum (the accumulator store) on the others.
// The shadows drain while the next tile runs: group (i, cg) of tile q at step (q+1) P + L + n, n the
// group's diagonal i + cg L (distinct while R <= L) -- into the accumulator store on all panels but the
// last, and on the last rounded once to c_t into the C block (for MUL_AH, conj(B) as B enters, in ba_t,
// and the exact imaginary sum negated before the rounding).
//
// Python twin: waveflow.linalg.systolic_engine.EngineCore.  Types: wf_systolic_traits<FID>.
#ifndef WF_SYSTOLIC_ENGINE_CORE_TASK_H
#define WF_SYSTOLIC_ENGINE_CORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_fixed.h>
#include <ap_int.h>
#include "complex_utils.hpp"
#include "wf_lanes.h"
#include "wf_linalg_traits.h"
#include "wf_engine_job.h"

namespace wf_engine {

static inline int imax(int a, int b) { return a > b ? a : b; }

// One panel of one output: every tile, in one pipelined loop.  last: round into cc; otherwise into
// the accumulator store.  first: start each tile from zero, not from the store.
template <int MMAX, int KMAX, int NMAX, int L, int R, int C, int FORM, int LW, int FID>
static void panel(
    const typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, 1>::type a[MMAX][KMAX],
    const typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, 1>::type
        b[KMAX][NMAX / C][C / LW][LW],
    typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type cc[MMAX * NMAX / L],
    typename wf_systolic_traits<FID>::acc_t accs_r[(MMAX / R) * (NMAX / C)][R][C],
    typename wf_systolic_traits<FID>::acc_t accs_i[(MMAX / R) * (NMAX / C)][R][C], int m, int kp,
    int n, bool adj, bool first, bool last) {
#pragma HLS INLINE
    typedef wf_systolic_traits<FID> TR;
    typedef typename TR::a_t a_t;
    typedef typename TR::b_t b_t;
    typedef typename TR::c_t c_t;
    typedef typename TR::ba_t ba_t;
    typedef typename TR::p_t p_t;
    typedef typename TR::acc_t acc_t;
    const int G = R * C / L, CL = C / L;
    const int P = imax(imax(kp, G), L);
    const int nct = n / C, T = (m / R) * nct, ng = n / L;

    acc_t acc_r[R][C], acc_i[R][C], sh_r[R][C], sh_i[R][C];
    a_t ax_r[R][C], ax_i[R][C];
    ba_t bx_r[R][C], bx_i[R][C];
    bool fl[R][C], pv[R][C];  // the first flag, and "start from the store" (a later panel's tile)
    ap_uint<8> qt[R][C];      // the tile the flag starts, riding along the row with it
#pragma HLS ARRAY_PARTITION variable = acc_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = acc_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = sh_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = sh_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = ax_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = ax_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bx_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bx_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = fl complete dim = 0
#pragma HLS ARRAY_PARTITION variable = pv complete dim = 0
#pragma HLS ARRAY_PARTITION variable = qt complete dim = 0
    // the edges: row i at (tile qr, step kr, row tile rtr); column j at (tile qc, step kc, col tile ctc).
    // Narrow counters: each is at most a few hundred, and a 32-bit int costs a select, an adder and a
    // comparator of 32 bits per counter per cycle.
    typedef ap_uint<10> cnt_t;
    cnt_t kr[R], qr[R], rtr[R], ctr[R], kc[C], qc[C], ctc[C];
#pragma HLS ARRAY_PARTITION variable = kr complete
#pragma HLS ARRAY_PARTITION variable = qr complete
#pragma HLS ARRAY_PARTITION variable = rtr complete
#pragma HLS ARRAY_PARTITION variable = ctr complete
#pragma HLS ARRAY_PARTITION variable = kc complete
#pragma HLS ARRAY_PARTITION variable = qc complete
#pragma HLS ARRAY_PARTITION variable = ctc complete
    for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            acc_r[i][j] = 0;
            acc_i[i][j] = 0;
            sh_r[i][j] = 0;
            sh_i[i][j] = 0;
            ax_r[i][j] = 0;
            ax_i[i][j] = 0;
            bx_r[i][j] = 0;
            bx_i[i][j] = 0;
            fl[i][j] = false;
            pv[i][j] = false;
            qt[i][j] = 0;
        }
        kr[i] = 0;
        qr[i] = 0;
        rtr[i] = 0;
        ctr[i] = 0;
    }
    for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
        kc[j] = 0;
        qc[j] = 0;
        ctc[j] = 0;
    }
    cnt_t dq = 0, dn = 0, drt = 0, dct = 0, dwait = P + L;
    const int steps = T * P + imax(L + G, R + C - 1) + P;
STEP:
    for (int t = 0; t < steps; ++t) {
#pragma HLS LOOP_TRIPCOUNT max = (MMAX / R) * (NMAX / C) * KMAX + 3 * KMAX
#pragma HLS PIPELINE II = 1
        // 1. the drain: shadows latched on earlier steps
        if (dwait > 0) {
            --dwait;
        } else if (dq < T) {
            const int i = dn % R, cg = dn / R;  // the group of diagonal dn (R <= L)
            if (last) {
                c_t re[L], im[L];
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    const acc_t v = sh_i[i][cg * L + l];
                    re[l] = sh_r[i][cg * L + l];  // q_c: the one rounding
                    im[l] = adj ? acc_t(-v) : v;  // conj before the rounding
                }
                cc[(drt * R + i) * ng + dct * CL + cg] = wf_lanes::pack<c_t, L>(re, im);
            } else {
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    accs_r[dq][i][cg * L + l] = sh_r[i][cg * L + l];
                    accs_i[dq][i][cg * L + l] = sh_i[i][cg * L + l];
                }
            }
            if (dn == G - 1) {
                dn = 0;
                ++dq;
                dwait = P - G;
                if (dct == nct - 1) {
                    dct = 0;
                    ++drt;
                } else {
                    ++dct;
                }
            } else {
                ++dn;
            }
        }
        // 2. the elements, last first, so each reads its neighbours' values from the step before
        for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
            for (int j = C - 1; j >= 0; --j) {
#pragma HLS UNROLL
                a_t ar = 0, ai = 0;
                ba_t vr = 0, vi = 0;
                bool f = false, v = false;
                ap_uint<8> q = 0;
                if (j == 0) {
                    if (t >= i && qr[i] <= T) {
                        f = kr[i] == 0;
                        q = qr[i];
                        v = !first && qr[i] < T;
                        if (qr[i] < T && kr[i] < kp) {
                            wf_lanes::get<a_t, 1>(a[rtr[i] * R + i][kr[i]], 0, ar, ai);
                        }
                    }
                } else {
                    ar = ax_r[i][j - 1];
                    ai = ax_i[i][j - 1];
                    f = fl[i][j - 1];
                    v = pv[i][j - 1];
                    q = qt[i][j - 1];
                }
                if (i == 0) {
                    if (t >= j && qc[j] < T && kc[j] < kp) {
                        b_t br, bi;
                        wf_lanes::get<b_t, 1>(b[kc[j]][ctc[j]][j / LW][j % LW], 0, br, bi);
                        vr = br;
                        vi = adj ? ba_t(-ba_t(bi)) : ba_t(bi);  // conj(B) as it enters
                    }
                } else {
                    vr = bx_r[i - 1][j];
                    vi = bx_i[i - 1][j];
                }
                acc_t pr = 0, pi = 0;
                if (FORM == 3) {
                    p_t re3, im3;
                    complex_utils::cmult3_parts(ar, ai, vr, vi, re3, im3);
                    pr = re3;
                    pi = im3;
                } else {
                    pr = ar * vr - ai * vi;
                    pi = ar * vi + ai * vr;
                }
                // a new tile starts from zero, or from its partial sum on a later panel; one adder
                acc_t base_r = acc_r[i][j], base_i = acc_i[i][j];
                if (f) {
                    sh_r[i][j] = acc_r[i][j];
                    sh_i[i][j] = acc_i[i][j];
                    base_r = v ? accs_r[q][i][j] : acc_t(0);
                    base_i = v ? accs_i[q][i][j] : acc_t(0);
                }
                acc_r[i][j] = base_r + pr;
                acc_i[i][j] = base_i + pi;
                ax_r[i][j] = ar;
                ax_i[i][j] = ai;
                fl[i][j] = f;
                pv[i][j] = v;
                qt[i][j] = q;
                bx_r[i][j] = vr;
                bx_i[i][j] = vi;
            }
        }
        // 3. the edge counters (tiles: row tiles outer, column tiles inner)
        for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
            if (t >= i && qr[i] <= T) {
                if (kr[i] == P - 1) {
                    kr[i] = 0;
                    ++qr[i];
                    if (ctr[i] == nct - 1) {
                        ctr[i] = 0;
                        ++rtr[i];
                    } else {
                        ++ctr[i];
                    }
                } else {
                    ++kr[i];
                }
            }
        }
        for (int j = 0; j < C; ++j) {
#pragma HLS UNROLL
            if (t >= j && qc[j] < T) {
                if (kc[j] == P - 1) {
                    kc[j] = 0;
                    ++qc[j];
                    if (ctc[j] == nct - 1) {
                        ctc[j] = 0;
                    } else {
                        ++ctc[j];
                    }
                } else {
                    ++kc[j];
                }
            }
        }
    }
}

}  // namespace wf_engine

template <int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int FORM, int SD, int LW,
          int FID>
static void systolic_engine_core_task(
    hls::stream<ap_uint<64> >& job_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, 1>::type
                              [MMAX][KMAX],
                          SD>& a_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, 1>::type
                              [KMAX][NMAX / C][C / LW][LW],
                          SD>& b_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [MMAX * NMAX / L],
                          SD>& c_blk) {
    typedef wf_systolic_traits<FID> TR;
    typedef typename wf_lanes::group<typename TR::a_t, 1>::type ae_t;
    typedef typename wf_lanes::group<typename TR::b_t, 1>::type be_t;
    typedef typename wf_lanes::group<typename TR::c_t, L>::type c_grp;
    typedef typename TR::acc_t acc_t;
    static_assert((L & (L - 1)) == 0 && C % L == 0 && NMAX % C == 0 && MMAX % R == 0 && R <= L,
                  "need L a power of two, L | C, C | NMAX, R | MMAX and R <= L");
    const int TMAX = (MMAX / R) * (NMAX / C), CGN = MMAX * NMAX / L;
    EngineJob j;
    j.template read_stream<64>(job_in);
    const bool adj = j.op == 2;
    const int m = j.m, kp = j.kp, n = j.n, np = j.npanels, nb = j.nb;
    // the partial sums of every tile of one output, between panels
    acc_t accs_r[TMAX][R][C], accs_i[TMAX][R][C];
#pragma HLS ARRAY_PARTITION variable = accs_r dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = accs_r dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = accs_i dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = accs_i dim = 3 complete
#pragma HLS BIND_STORAGE variable = accs_r type = ram_s2p impl = lutram
#pragma HLS BIND_STORAGE variable = accs_i type = ram_s2p impl = lutram
    // ONE call site of the array (each would be a copy of it in hardware): per output the C block is
    // held across its panels -- written on the last, the accumulator store on the others -- and
    // every panel takes an X block and a B block (with keep_a the A loader repeats its one A).
OUTS:
    for (int ob = 0; ob < nb; ++ob) {
#pragma HLS LOOP_TRIPCOUNT max = 4
        hls::write_lock<c_grp[CGN]> cc(c_blk);
    PANELS:
        for (int p = 0; p < np; ++p) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
            hls::read_lock<ae_t[MMAX][KMAX]> a(a_blk);
            hls::read_lock<be_t[KMAX][NMAX / C][C / LW][LW]> b(b_blk);
            wf_engine::panel<MMAX, KMAX, NMAX, L, R, C, FORM, LW, FID>(
                a, b, cc, accs_r, accs_i, m, kp, n, adj, p == 0, p == np - 1);
        }
    }
}

#endif  // WF_SYSTOLIC_ENGINE_CORE_TASK_H
