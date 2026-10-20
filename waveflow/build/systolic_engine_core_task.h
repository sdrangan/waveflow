// systolic_engine_core_task.h -- the systolic engine's core: a streaming systolic array, continuous
// across panels, outputs and jobs; and the collector that turns its output into C blocks.
//
// systolic_engine_core_task: one firing is one job (EngineJob): per output, per panel, it holds the X
// block (A, or A^T for MUL_AH) and the B block and sweeps them in place -- no copy: the blocks are
// banked as the array reads them (X a bank per row, B a bank per array column).  keep_a is the A
// loader's: it repeats its one A per output, so the core takes an X block per panel either way (one
// call site of the array).
//
// The array has R x C complex processing elements, output-stationary.  Tiles follow each other every
// P = max(kp, R C / L, L) steps (a tile shorter than P is padded with zero steps, which add nothing):
// at tile step kk the edges read X[i][kk] for every row i and B[kk][j] for every column j at once, and
// delay lines hand row i its value i steps later and column j its value j steps later, so element
// (i, j) meets X[i][kk] and B[kk][j] at one step and a panel's reads all fall in its own T P steps.  A
// "first" flag rides with the X input: when it reaches an element, the element latches its sum into a
// shadow register and starts the next tile at once -- from zero on an output's first panel, from its
// partial sum (the accumulator store) on the others.
//
// Continuous (plans/systolic_characterization.md, "continuous panels"): within a job, the array, the
// delay lines and the drain carry over from panel to panel and output to output, so the next tile
// starts the step after this one ends, and tile g drains while tile g + 1 runs: group (i, cg) at
// L + n steps after tile g + 1 starts, n = i + cg L its diagonal (distinct while R <= L) -- into the
// accumulator store on all panels but an output's last, and on the last rounded once to c_t onto c_out
// (for MUL_AH, conj(B) as B enters, in ba_t, and the exact imaginary sum negated before the rounding).
// One tile (T = 1): the next panel's tile is the same tile, so the elements keep accumulating -- no
// latch into the store, no read back.  Two waits remain: a gap before a panel whose tiles read partial
// sums written too recently (T = 2, 3 at the centre), and an empty tile at the end of each job that runs
// its last tile out (L + G steps).  (Carrying the state from job to job as `static` registers made
// Vitis 2025.1 csynth run out of memory -- 6.7 GB against 0.4 -- so each firing starts the array clean.)
//
// systolic_engine_collect_task: one job per firing; per output, the m n / L groups off c_in in the
// core's drain order (row tiles, column tiles, diagonals), each into its place in a C block.
//
// Python twins: waveflow.linalg.systolic_engine.EngineCore / EngineCollect.  Types: wf_systolic_traits<FID>.
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

// Where a tile's sums go when it drains: nowhere (the empty tile that runs the last one out, or a
// lone tile that keeps accumulating), the accumulator store, or c_out.
enum { D_NONE = 0, D_ACC = 1, D_C = 2 };
// How a tile starts: from zero (an output's first panel), from its partial sum in the store, or --
// one tile, a later panel -- from the sum the element holds.
enum { S_ZERO = 0, S_STORE = 1, S_KEEP = 2 };

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
    hls::stream<ap_uint<64> >& col_job,
    hls::stream<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type>& c_out) {
    typedef wf_systolic_traits<FID> TR;
    typedef typename wf_lanes::group<typename TR::a_t, 1>::type ae_t;
    typedef typename wf_lanes::group<typename TR::b_t, 1>::type be_t;
    typedef typename TR::a_t a_t;
    typedef typename TR::b_t b_t;
    typedef typename TR::c_t c_t;
    typedef typename TR::ba_t ba_t;
    typedef typename TR::p_t p_t;
    typedef typename TR::acc_t acc_t;
    typedef ap_uint<10> cnt_t;  // narrow counters: a 32-bit int is a 32-bit select, adder and compare
    static_assert((L & (L - 1)) == 0 && C % L == 0 && NMAX % C == 0 && MMAX % R == 0 && R <= L,
                  "need L a power of two, L | C, C | NMAX, R | MMAX and R <= L");
    const int TMAX = (MMAX / R) * (NMAX / C), G = R * C / L;
    EngineJob j;
    j.template read_stream<64>(job_in);
    j.template write_stream<64>(col_job);
    const bool adj = j.op == 2;
    const int m = j.m, kp = j.kp, n = j.n, np = j.npanels, nb = j.nb;
    const int P = wf_engine::imax(wf_engine::imax(kp, G), L);
    const int nct = n / C, T = (m / R) * nct, TP = T * P;
    // Panel p's tile q drains into the store at L + n steps after tile q + 1 starts, and panel p + 1's
    // tile q reads it (T - 1) P steps later: with few tiles that is too close for the store (and the
    // pipeline's depth), so the panel's last tile runs longer by the difference.
    const bool lone = T == 1;
    const int gap = lone ? 0 : wf_engine::imax(0, L + 9 - (T - 1) * P);

    // the partial sums of every tile of one output, between its panels (all within this firing)
    acc_t accs_r[TMAX][R][C], accs_i[TMAX][R][C];
#pragma HLS ARRAY_PARTITION variable = accs_r dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = accs_r dim = 3 complete
#pragma HLS ARRAY_PARTITION variable = accs_i dim = 2 complete
#pragma HLS ARRAY_PARTITION variable = accs_i dim = 3 complete
#pragma HLS BIND_STORAGE variable = accs_r type = ram_s2p impl = lutram
#pragma HLS BIND_STORAGE variable = accs_i type = ram_s2p impl = lutram

    // The array, its delay lines and the drain: carried across the job's panels and outputs.
    acc_t acc_r[R][C], acc_i[R][C], sh_r[R][C], sh_i[R][C];
    a_t ax_r[R][C], ax_i[R][C];
    ba_t bx_r[R][C], bx_i[R][C];
    bool fl[R][C];            // the first flag
    ap_uint<2> pv[R][C];      // and how the tile it starts begins (S_*)
    ap_uint<8> qt[R][C];      // the tile the flag starts, riding along the row with it
    a_t xd_r[R][R], xd_i[R][R];  // row i's delay line: xd[i][d], d <= i
    bool xd_f[R][R];
    ap_uint<2> xd_v[R][R];
    ap_uint<8> xd_q[R][R];
    ba_t bd_r[C][C], bd_i[C][C];  // column j's: bd[j][d], d <= j
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
#pragma HLS ARRAY_PARTITION variable = xd_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = xd_i complete dim = 0
#pragma HLS ARRAY_PARTITION variable = xd_f complete dim = 0
#pragma HLS ARRAY_PARTITION variable = xd_v complete dim = 0
#pragma HLS ARRAY_PARTITION variable = xd_q complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bd_r complete dim = 0
#pragma HLS ARRAY_PARTITION variable = bd_i complete dim = 0
    // The drain: the started tile (pending), the one queued to drain L steps after its successor
    // started, and the one draining a group a step.  Consecutive drains never overlap (G <= P).
    ap_uint<2> pn_dest = wf_engine::D_NONE, qu_dest = wf_engine::D_NONE, ac_dest = wf_engine::D_NONE;
    ap_uint<8> pn_q = 0, qu_q = 0, ac_q = 0;
    bool pn_adj = false, qu_adj = false, ac_adj = false, qu_on = false, ac_on = false;
    cnt_t qu_wait = 0, dn = 0;
    for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
        for (int jj = 0; jj < C; ++jj) {
#pragma HLS UNROLL
            acc_r[i][jj] = 0;
            acc_i[i][jj] = 0;
            sh_r[i][jj] = 0;
            sh_i[i][jj] = 0;
            ax_r[i][jj] = 0;
            ax_i[i][jj] = 0;
            bx_r[i][jj] = 0;
            bx_i[i][jj] = 0;
            fl[i][jj] = false;
            pv[i][jj] = wf_engine::S_ZERO;
            qt[i][jj] = 0;
        }
        for (int d = 0; d < R; ++d) {
#pragma HLS UNROLL
            xd_r[i][d] = 0;
            xd_i[i][d] = 0;
            xd_f[i][d] = false;
            xd_v[i][d] = wf_engine::S_ZERO;
            xd_q[i][d] = 0;
        }
    }
    for (int jj = 0; jj < C; ++jj) {
#pragma HLS UNROLL
        for (int d = 0; d < C; ++d) {
#pragma HLS UNROLL
            bd_r[jj][d] = 0;
            bd_i[jj][d] = 0;
        }
    }

    // ONE call site of the array (each would be a copy of it in hardware): every panel of every output
    // takes an X block and a B block (with keep_a the A loader repeats its one A).
OUTS:
    for (int ob = 0; ob < nb; ++ob) {
#pragma HLS LOOP_TRIPCOUNT max = 4
    PANELS:
        for (int p = 0; p < np; ++p) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
            hls::read_lock<ae_t[MMAX][KMAX]> a(a_blk);
            hls::read_lock<be_t[KMAX][NMAX / C][C / LW][LW]> b(b_blk);
            const bool first = p == 0, lastp = p == np - 1;
            const bool run_out = lastp && ob == nb - 1;  // the job's last panel: run it out
            const int steps = TP + (lastp ? 0 : gap) + (run_out ? L + G : 0);
            cnt_t kk = 0, ct = 0, rt = 0;
            ap_uint<8> q = 0;  // the edge: tile q (row tile rt, column tile ct), tile step kk
        STEP:
            for (int t = 0; t < steps; ++t) {
#pragma HLS LOOP_TRIPCOUNT max = (MMAX / R) * (NMAX / C) * KMAX + 3 * KMAX
#pragma HLS PIPELINE II = 1
                const bool real = t < TP;  // a tile step; past it, the gap or the empty tile
                const bool start = (real && kk == 0) || (run_out && t == TP);
                // 1. the drain, before the elements overwrite any shadow
                if (qu_on && qu_wait == 0) {
                    ac_on = true;
                    ac_dest = qu_dest;
                    ac_q = qu_q;
                    ac_adj = qu_adj;
                    dn = 0;
                    qu_on = false;
                } else if (qu_on) {
                    --qu_wait;
                }
                if (ac_on) {
                    const int i = dn % R, cg = dn / R;  // the group of diagonal dn (R <= L)
                    if (ac_dest == wf_engine::D_C) {
                        c_t re[L], im[L];
                        for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                            const acc_t v = sh_i[i][cg * L + l];
                            re[l] = sh_r[i][cg * L + l];     // q_c: the one rounding
                            im[l] = ac_adj ? acc_t(-v) : v;  // conj before the rounding
                        }
                        c_out.write(wf_lanes::pack<c_t, L>(re, im));
                    } else if (ac_dest == wf_engine::D_ACC) {
                        for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                            accs_r[ac_q][i][cg * L + l] = sh_r[i][cg * L + l];
                            accs_i[ac_q][i][cg * L + l] = sh_i[i][cg * L + l];
                        }
                    }
                    if (dn == G - 1) {
                        ac_on = false;
                    } else {
                        ++dn;
                    }
                }
                if (start) {  // the tile before the one started now drains L steps from now
                    qu_on = true;
                    qu_wait = L - 1;
                    qu_dest = pn_dest;
                    qu_q = pn_q;
                    qu_adj = pn_adj;
                    pn_dest = !real  ? wf_engine::D_NONE
                              : lastp ? wf_engine::D_C
                              : lone  ? wf_engine::D_NONE
                                      : wf_engine::D_ACC;
                    pn_q = q;
                    pn_adj = adj;
                }
                // 2. the edges: this step's values for every row and column, into the delay lines
                const bool rd = real && kk < kp;
                for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
                    for (int d = i; d > 0; --d) {
#pragma HLS UNROLL
                        xd_r[i][d] = xd_r[i][d - 1];
                        xd_i[i][d] = xd_i[i][d - 1];
                        xd_f[i][d] = xd_f[i][d - 1];
                        xd_v[i][d] = xd_v[i][d - 1];
                        xd_q[i][d] = xd_q[i][d - 1];
                    }
                    a_t ar = 0, ai = 0;
                    if (rd) wf_lanes::get<a_t, 1>(a[rt * R + i][kk], 0, ar, ai);
                    xd_r[i][0] = ar;
                    xd_i[i][0] = ai;
                    xd_f[i][0] = start;
                    xd_v[i][0] = (!real || first) ? wf_engine::S_ZERO
                                 : lone               ? wf_engine::S_KEEP
                                                      : wf_engine::S_STORE;
                    xd_q[i][0] = q;
                }
                for (int jj = C - 1; jj >= 0; --jj) {
#pragma HLS UNROLL
                    for (int d = jj; d > 0; --d) {
#pragma HLS UNROLL
                        bd_r[jj][d] = bd_r[jj][d - 1];
                        bd_i[jj][d] = bd_i[jj][d - 1];
                    }
                    ba_t vr = 0, vi = 0;
                    if (rd) {
                        b_t br, bi;
                        wf_lanes::get<b_t, 1>(b[kk][ct][jj / LW][jj % LW], 0, br, bi);
                        vr = br;
                        vi = adj ? ba_t(-ba_t(bi)) : ba_t(bi);  // conj(B) as it enters
                    }
                    bd_r[jj][0] = vr;
                    bd_i[jj][0] = vi;
                }
                // 3. the elements, last first, so each reads its neighbours' values from the step before
                for (int i = R - 1; i >= 0; --i) {
#pragma HLS UNROLL
                    for (int jj = C - 1; jj >= 0; --jj) {
#pragma HLS UNROLL
                        a_t ar, ai;
                        ba_t vr, vi;
                        bool f;
                        ap_uint<2> v;
                        ap_uint<8> qq;
                        if (jj == 0) {
                            ar = xd_r[i][i];
                            ai = xd_i[i][i];
                            f = xd_f[i][i];
                            v = xd_v[i][i];
                            qq = xd_q[i][i];
                        } else {
                            ar = ax_r[i][jj - 1];
                            ai = ax_i[i][jj - 1];
                            f = fl[i][jj - 1];
                            v = pv[i][jj - 1];
                            qq = qt[i][jj - 1];
                        }
                        if (i == 0) {
                            vr = bd_r[jj][jj];
                            vi = bd_i[jj][jj];
                        } else {
                            vr = bx_r[i - 1][jj];
                            vi = bx_i[i - 1][jj];
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
                        // a new tile starts from zero, from its partial sum in the store, or (one
                        // tile) from the sum it holds; one adder
                        acc_t base_r = acc_r[i][jj], base_i = acc_i[i][jj];
                        if (f) {
                            sh_r[i][jj] = acc_r[i][jj];
                            sh_i[i][jj] = acc_i[i][jj];
                            if (v == wf_engine::S_STORE) {
                                base_r = accs_r[qq][i][jj];
                                base_i = accs_i[qq][i][jj];
                            } else if (v == wf_engine::S_ZERO) {
                                base_r = 0;
                                base_i = 0;
                            }
                        }
                        acc_r[i][jj] = base_r + pr;
                        acc_i[i][jj] = base_i + pi;
                        ax_r[i][jj] = ar;
                        ax_i[i][jj] = ai;
                        fl[i][jj] = f;
                        pv[i][jj] = v;
                        qt[i][jj] = qq;
                        bx_r[i][jj] = vr;
                        bx_i[i][jj] = vi;
                    }
                }
                // 4. the edge counters (tiles: row tiles outer, column tiles inner)
                if (real) {
                    if (kk == P - 1) {
                        kk = 0;
                        ++q;
                        if (ct == nct - 1) {
                            ct = 0;
                            ++rt;
                        } else {
                            ++ct;
                        }
                    } else {
                        ++kk;
                    }
                }
            }
        }
    }
}

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int SD, int FID>
static void systolic_engine_collect_task(
    hls::stream<ap_uint<64> >& job_in,
    hls::stream<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type>& c_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [MMAX * NMAX / L],
                          SD>& c_blk) {
    typedef typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type c_grp;
    typedef ap_uint<10> cnt_t;
    const int CGN = MMAX * NMAX / L, G = R * C / L, CL = C / L;
    EngineJob j;
    j.template read_stream<64>(job_in);
    const int m = j.m, n = j.n, nb = j.nb;
    const int ng = n / L, nct = n / C, total = m * n / L;
OUTS:
    for (int ob = 0; ob < nb; ++ob) {
#pragma HLS LOOP_TRIPCOUNT max = 4
        hls::write_lock<c_grp[CGN]> cc(c_blk);
        cnt_t rt = 0, ct = 0, i = 0, cg = 0;  // the drain order: row tiles, column tiles, diagonals
    GROUPS:
        for (int k = 0; k < total; ++k) {
#pragma HLS LOOP_TRIPCOUNT max = MMAX * NMAX / L
#pragma HLS PIPELINE II = 1
            cc[(rt * R + i) * ng + ct * CL + cg] = c_in.read();
            if (i == R - 1) {
                i = 0;
                if (cg == CL - 1) {
                    cg = 0;
                    if (ct == nct - 1) {
                        ct = 0;
                        ++rt;
                    } else {
                        ++ct;
                    }
                } else {
                    ++cg;
                }
            } else {
                ++i;
            }
        }
    }
}

#endif  // WF_SYSTOLIC_ENGINE_CORE_TASK_H
