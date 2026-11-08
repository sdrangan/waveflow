// core_bench_tasks.h -- TEST ONLY: a feeder and a drain around a systolic core, so a composite of
// cores can be C-simulated and synthesized with plain 64-bit streams at its boundary.
//
// core_feed_task:  s_cmd carries one SystolicCmd word per job, forwarded to the core and the drain;
//                  s_in carries X (A, or A^T for A^H) and then the nb matrices B as lane groups, each group
//                  in NW = ceil(2 W L / 64) words, low word first.
// core_drain_task: writes each C the same way on s_out.
// Python twins: tests/linalg/_core_bench.py (CoreFeed, CoreDrain).
#ifndef WF_TEST_CORE_BENCH_TASKS_H
#define WF_TEST_CORE_BENCH_TASKS_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "systolic_core_task.h"

namespace core_bench_io {

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

}  // namespace core_bench_io

template <int MMAX, int KMAX, int NMAX, int L, int SD, int FID>
static void core_feed_task(
    hls::stream<ap_uint<64> >& s_cmd, hls::stream<ap_uint<64> >& s_in,
    hls::stream<ap_uint<64> >& core_cmd, hls::stream<ap_uint<64> >& drain_cmd,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, L>::type
                              [wf_systolic::blk<MMAX, KMAX, L>::n],
                          SD>& a_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, L>::type
                              [wf_systolic::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk) {
    typedef typename wf_systolic_traits<FID>::a_t a_t;
    typedef typename wf_systolic_traits<FID>::b_t b_t;
    const int AG = wf_systolic::blk<MMAX, KMAX, L>::n;
    const int BG = wf_systolic::blk<KMAX, NMAX, L>::n;
    const ap_uint<64> w = s_cmd.read();
    core_cmd.write(w);
    drain_cmd.write(w);
    const SystolicCmd cmd = SystolicCmd::unpack_from_uint(w);
    const int m = cmd.m, k = cmd.k, n = cmd.n, nb = cmd.nb;
    {
        hls::write_lock<typename wf_lanes::group<a_t, L>::type[AG]> a(a_blk);
        core_bench_io::read_groups<a_t, L, AG>(s_in, a, (m * k + L - 1) / L);
    }
JOB:
    for (int b = 0; b < nb; ++b) {
#pragma HLS LOOP_TRIPCOUNT max=1
        hls::write_lock<typename wf_lanes::group<b_t, L>::type[BG]> bb(b_blk);
        core_bench_io::read_groups<b_t, L, BG>(s_in, bb, k * n / L);
    }
}

template <int MMAX, int KMAX, int NMAX, int L, int SD, int FID>
static void core_drain_task(
    hls::stream<ap_uint<64> >& s_cmd,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [wf_systolic::blk<MMAX, NMAX, L>::n],
                          SD>& c_blk,
    hls::stream<ap_uint<64> >& s_out) {
    typedef typename wf_systolic_traits<FID>::c_t c_t;
    const int CG = wf_systolic::blk<MMAX, NMAX, L>::n;
    const SystolicCmd cmd = SystolicCmd::unpack_from_uint(s_cmd.read());
    const int m = cmd.m, n = cmd.n, nb = cmd.nb;
JOB:
    for (int b = 0; b < nb; ++b) {
#pragma HLS LOOP_TRIPCOUNT max=1
        hls::read_lock<typename wf_lanes::group<c_t, L>::type[CG]> c(c_blk);
        core_bench_io::write_groups<c_t, L, CG>(c, s_out, m * n / L);
    }
}

#endif  // WF_TEST_CORE_BENCH_TASKS_H
