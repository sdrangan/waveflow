// systolic_engine_load_task.h -- the systolic engine's two loaders: one job per firing each.
//
// systolic_engine_load_a_task lands A in the X block [MMAX][KMAX] -- a bank per row, so each array
// row reads its own -- a value a cycle (a word every LW cycles: the pattern for one element per
// iteration).  For MUL_AH the stored A is kp x m and the block takes its transpose, X = A^T, at the
// same rate.  It fills a block per output and panel; with keep_a the one A arrives once and is
// repeated from a local copy into each output's block.
//
// systolic_engine_load_b_task lands B in its block [KMAX][NMAX/C][C/LW][LW] -- a bank per array
// column, (j/LW, j%LW) -- a word a cycle: n is a multiple of C, so a word's LW values never cross a
// row and fall in lanes 0..LW-1 of one (row, tile, group), banks the compiler can tell apart.
// Python twins: waveflow.linalg.systolic_engine.EngineLoadA / EngineLoadB.
#ifndef WF_SYSTOLIC_ENGINE_LOAD_TASK_H
#define WF_SYSTOLIC_ENGINE_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_linalg_traits.h"
#include "wf_engine_job.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int SD, int LW,
          int FID, int IOID>
static void systolic_engine_load_a_task(
    hls::stream<ap_uint<64> >& job_in, hls::stream<streamutils::framed_word<WBW> >& s_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, 1>::type
                              [MMAX][KMAX],
                          SD>& blk) {
    typedef wf_systolic_io_traits<IOID> IO;
    typedef typename wf_systolic_traits<FID>::a_t a_t;
    typedef typename wf_lanes::group<a_t, 1>::type e_t;
    static_assert(LW == IO::a_mem::template lane_capacity<WBW>(), "LW: values per word");
    EngineJob j;
    j.template read_stream<64>(job_in);
    const int m = j.m, kp = j.kp;
    const bool tr = j.op == 2;  // MUL_AH: stored kp x m, the block takes X = A^T
    const bool keep = j.keep_a != 0;
    const int count = int(j.nb) * int(j.npanels);  // a block per output and panel, keep_a or not
    const int scols = tr ? m : kp;  // the stored matrix's row length
    // With keep_a the one A arrives once and is repeated from here into each output's block: the
    // copy runs beside the core's sweep, so the core takes an X block per panel either way.
    e_t keep_x[MMAX][KMAX];
#pragma HLS ARRAY_PARTITION variable = keep_x dim = 1 complete
    streamutils::tlast_status tl;
MATS:
    for (int q = 0; q < count; ++q) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
        const bool fresh = !keep || q == 0;
        hls::write_lock<e_t[MMAX][KMAX]> x(blk);
        typename IO::a_mem::value_type buf[LW];
        int sr = 0, sc = 0, l = 0;  // the element's stored (row, column) and lane in its word
    ELEMS:
        for (int e = 0; e < m * kp; ++e) {
#pragma HLS LOOP_TRIPCOUNT max = MMAX * KMAX
#pragma HLS PIPELINE II = 1
            const int xr = tr ? sc : sr, xc = tr ? sr : sc;  // its place in X (A^T for MUL_AH)
            e_t g = 0;
            if (fresh) {
                if (l == 0) IO::a_mem::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
                const a_t re = buf[l].real(), im = buf[l].imag();
                wf_lanes::set<a_t, 1>(g, 0, re, im);
                if (keep) keep_x[xr][xc] = g;
            } else {
                g = keep_x[xr][xc];
            }
            x[xr][xc] = g;
            l = (l == LW - 1) ? 0 : l + 1;
            if (sc == scols - 1) {
                sc = 0;
                ++sr;
            } else {
                ++sc;
            }
        }
    }
}

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int SD, int LW,
          int FID, int IOID>
static void systolic_engine_load_b_task(
    hls::stream<ap_uint<64> >& job_in, hls::stream<streamutils::framed_word<WBW> >& s_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, 1>::type
                              [KMAX][NMAX / C][C / LW][LW],
                          SD>& blk) {
    typedef wf_systolic_io_traits<IOID> IO;
    typedef typename wf_systolic_traits<FID>::b_t b_t;
    typedef typename wf_lanes::group<b_t, 1>::type e_t;
    static_assert(LW == IO::b_mem::template lane_capacity<WBW>(), "LW: values per word");
    const int CG = C / LW;
    EngineJob j;
    j.template read_stream<64>(job_in);
    const int kp = j.kp, n = j.n;
    const int count = j.nb * j.npanels;
    streamutils::tlast_status tl;
MATS:
    for (int q = 0; q < count; ++q) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
        hls::write_lock<e_t[KMAX][NMAX / C][C / LW][LW]> x(blk);
        int kk = 0, ct = 0, cg = 0;  // the word's row, column tile and lane group in the tile
    WORDS:
        for (int w = 0; w < kp * n / LW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = KMAX * NMAX / 2
#pragma HLS PIPELINE II = 1
            typename IO::b_mem::value_type buf[LW];
            IO::b_mem::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
            for (int l = 0; l < LW; ++l) {
#pragma HLS UNROLL
                const b_t re = buf[l].real(), im = buf[l].imag();
                e_t g = 0;
                wf_lanes::set<b_t, 1>(g, 0, re, im);
                x[kk][ct][cg][l] = g;
            }
            if (cg == CG - 1) {
                cg = 0;
                if (ct == n / C - 1) {
                    ct = 0;
                    ++kk;
                } else {
                    ++ct;
                }
            } else {
                ++cg;
            }
        }
    }
}

#endif  // WF_SYSTOLIC_ENGINE_LOAD_TASK_H
