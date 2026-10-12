// cg_core_bench_tasks.h -- TEST ONLY: a feeder and a drain around a CG vector core, so a composite
// of cores can be C-simulated and synthesized with plain 64-bit streams at its boundary.
//
// cg_feed_task:  s_cmd carries one CgVectorCmd word per job, forwarded to the core and the drain;
//                s_in carries B and then the nit matrices S as lane groups, each group in
//                NW = ceil(2 W L / 64) words, low word first.
// cg_drain_task: writes the nit matrices P (P_0 .. P_nit-1) and then X the same way on s_out.
// Python twins: tests/linalg/_cg_core_bench.py (CgFeed, CgDrain).
#ifndef WF_TEST_CG_CORE_BENCH_TASKS_H
#define WF_TEST_CG_CORE_BENCH_TASKS_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "cg_vector_task.h"

namespace cg_bench_io {

template <class T, int L>
struct words {
    static const int GW = 2 * T::width * L;
    static const int n = (GW + 63) / 64;
};

template <class T, int L, int NG>
static void read_groups(hls::stream<ap_uint<64> >& s, typename wf_lanes::group<T, L>::type blk[NG],
                        int ngroups) {
#pragma HLS INLINE
    const int NW = words<T, L>::n;
GROUPS:
    for (int g = 0; g < ngroups; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=NG
        ap_uint<64 * NW> buf = 0;
        for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            buf.range(64 * w + 63, 64 * w) = s.read();
        }
        blk[g] = buf.range(words<T, L>::GW - 1, 0);
    }
}

template <class T, int L, int NG>
static void write_groups(const typename wf_lanes::group<T, L>::type blk[NG],
                         hls::stream<ap_uint<64> >& s, int ngroups) {
#pragma HLS INLINE
    const int NW = words<T, L>::n;
GROUPS:
    for (int g = 0; g < ngroups; ++g) {
#pragma HLS LOOP_TRIPCOUNT max=NG
        ap_uint<64 * NW> buf = blk[g];
        for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            s.write(buf.range(64 * w + 63, 64 * w));
        }
    }
}

}  // namespace cg_bench_io

template <int KMAX, int NMAX, int NITMAX, int L, int SD, int FID>
static void cg_feed_task(
    hls::stream<ap_uint<64> >& s_cmd, hls::stream<ap_uint<64> >& s_in,
    hls::stream<ap_uint<64> >& core_cmd, hls::stream<ap_uint<64> >& drain_cmd,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::b_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::s_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& s_blk) {
    typedef typename wf_cg_traits<FID>::b_t b_t;
    typedef typename wf_cg_traits<FID>::s_t s_t;
    const int BG = wf_cg::blk<KMAX, NMAX, L>::n;
    const ap_uint<64> w = s_cmd.read();
    core_cmd.write(w);
    drain_cmd.write(w);
    const CgVectorCmd cmd = CgVectorCmd::unpack_from_uint(w);
    const int nit = cmd.nit, groups = cmd.k * cmd.n / L;
    {
        hls::write_lock<typename wf_lanes::group<b_t, L>::type[BG]> b(b_blk);
        cg_bench_io::read_groups<b_t, L, BG>(s_in, b, groups);
    }
ITERS:
    for (int it = 0; it < nit; ++it) {
#pragma HLS LOOP_TRIPCOUNT max=NITMAX
        hls::write_lock<typename wf_lanes::group<s_t, L>::type[BG]> s(s_blk);
        cg_bench_io::read_groups<s_t, L, BG>(s_in, s, groups);
    }
}

template <int KMAX, int NMAX, int NITMAX, int L, int SD, int FID>
static void cg_drain_task(
    hls::stream<ap_uint<64> >& s_cmd,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::p_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& p_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::x_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& x_blk,
    hls::stream<ap_uint<64> >& s_out) {
    typedef typename wf_cg_traits<FID>::p_t p_t;
    typedef typename wf_cg_traits<FID>::x_t x_t;
    const int BG = wf_cg::blk<KMAX, NMAX, L>::n;
    const CgVectorCmd cmd = CgVectorCmd::unpack_from_uint(s_cmd.read());
    const int nit = cmd.nit, groups = cmd.k * cmd.n / L;
ITERS:
    for (int it = 0; it < nit; ++it) {
#pragma HLS LOOP_TRIPCOUNT max=NITMAX
        hls::read_lock<typename wf_lanes::group<p_t, L>::type[BG]> p(p_blk);
        cg_bench_io::write_groups<p_t, L, BG>(p, s_out, groups);
    }
    {
        hls::read_lock<typename wf_lanes::group<x_t, L>::type[BG]> x(x_blk);
        cg_bench_io::write_groups<x_t, L, BG>(x, s_out, groups);
    }
}

#endif  // WF_TEST_CG_CORE_BENCH_TASKS_H
