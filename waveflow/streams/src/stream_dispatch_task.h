#ifndef WAVEFLOW_STREAMS_STREAM_DISPATCH_TASK_H
#define WAVEFLOW_STREAMS_STREAM_DISPATCH_TASK_H
// stream_dispatch_task.h -- StreamDispatch's hls::task body (waveflow/streams/dispatch.py), framework.
//
// ONE FIRING = ONE ACTION, or nothing:
//   1. a word on s_free: worker idx's credit back (a word naming no worker, or one with nothing
//      outstanding, is read and ignored) -- credits first, they are what unblocks;
//   2. else a job waiting on s_in and a worker holding a credit: the first worker in the search order
//      takes one credit and the job, whole -- NB TLAST bursts, word by word;
//   3. else nothing, and the task re-fires.
//
// Template parameters, in the order the composite generator supplies them (KernelTask: template_args,
// then one word type per `framed` port, in signature order):
//   W    the data width of every port;
//   NB   TLAST bursts a job (1: a message; 2: a header, then its payload; ...);
//   C    credits a worker: the jobs it may hold at once, 1 to 255;
//   RR   0: first free, the lowest-numbered worker holding a credit; 1: round-robin, the search starts
//        one past the last worker chosen;
//   TI   s_in's word type, TF s_free's, TO... each worker output's -- streamutils::axi4s_word<W> at a
//        top's port, streamutils::framed_word<W> on an internal FIFO.
//
// The outputs are the pack, so they come last in the signature (s_in, s_free, m_out_0, ...); their number
// is the pack's length.  Both inputs are tested with empty(), so a firing never blocks on an input that
// may stay empty -- and commits to a job on its first word: a job announced while still being produced
// stalls the block until it is done (the block's second rule).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

namespace stream_dispatch_body {

// One job, NB TLAST bursts, from s to d: a word a cycle.  The arbiter's copy loop, kept in its shape (a
// narrow count of TLASTs tested at the head) for the reasons stream_arbiter_task.h records, and INLINE
// so each output gets its own copy pipeline (one shared instance, its stream muxed per call, hung at RTL
// in the arbiter: plans/shared_kernel_rfsoc_timing.md).
template <int W, int NB, typename TS, typename TD>
static void copy_job(hls::stream<TS>& s, hls::stream<TD>& d) {
#pragma HLS INLINE
    ap_uint<8> b = 0;
COPY:
    while (b < NB) {
#pragma HLS PIPELINE II=1
        const TS x = s.read();
        streamutils::write_boundary_word<TD, W>(d, ap_uint<W>(x.data), bool(x.last));
        if (x.last) ++b;
    }
}

// The job to output k: the pack walked to its k-th stream.
template <int W, int NB, int K, typename TS>
static inline void copy_to(int, hls::stream<TS>&) {
#pragma HLS INLINE
}

template <int W, int NB, int K, typename TS, typename T0, typename... TR>
static inline void copy_to(int k, hls::stream<TS>& s, hls::stream<T0>& d0, hls::stream<TR>&... rest) {
#pragma HLS INLINE
    if (k == K) {
        copy_job<W, NB>(s, d0);
    } else {
        copy_to<W, NB, K + 1>(k, s, rest...);
    }
}

}  // namespace stream_dispatch_body

template <int W, int NB, int C, int RR, typename TI, typename TF, typename... TO>
static void stream_dispatch_task(hls::stream<TI>& s_in, hls::stream<TF>& s_free, hls::stream<TO>&... m_out) {
    constexpr int N = sizeof...(TO);
    static_assert(N >= 2 && N <= 16, "stream_dispatch_task: 2 to 16 workers");
    static_assert(NB >= 1 && NB <= 255, "stream_dispatch_task: a job is 1 to 255 bursts");
    static_assert(C >= 1 && C <= 255, "stream_dispatch_task: 1 to 255 credits a worker");
    // Jobs each worker holds now, 8 bits a worker, packed in one register so the reset is a scalar's.
    static ap_uint<8 * N> held = 0;
#pragma HLS reset variable=held
    // Round-robin: where the next search starts.
    static ap_uint<5> next_k = 0;
#pragma HLS reset variable=next_k

    if (!s_free.empty()) {
        const TF f = s_free.read();
        const ap_uint<W> idx = f.data;
    GIVE_BACK:
        for (int k = 0; k < N; ++k) {
#pragma HLS UNROLL
            const ap_uint<8> h = held.range(8 * k + 7, 8 * k);
            if (idx == ap_uint<W>(k) && h != 0) held.range(8 * k + 7, 8 * k) = ap_uint<8>(h - 1);
        }
        return;
    }
    if (s_in.empty()) return;

    int win = 0;
    bool found = false;
SEARCH:
    for (int j = 0; j < N; ++j) {
#pragma HLS UNROLL
        int k = RR ? int(next_k) + j : j;
        if (k >= N) k -= N;
        const ap_uint<8> h = held.range(8 * k + 7, 8 * k);
        if (!found && h < ap_uint<8>(C)) {
            win = k;
            found = true;
        }
    }
    if (!found) return;
TAKE:
    for (int k = 0; k < N; ++k) {
#pragma HLS UNROLL
        const ap_uint<8> h = held.range(8 * k + 7, 8 * k);
        if (k == win) held.range(8 * k + 7, 8 * k) = ap_uint<8>(h + 1);
    }
    if (RR) next_k = (win + 1 == N) ? 0 : win + 1;

    stream_dispatch_body::copy_to<W, NB, 0>(win, s_in, m_out...);
}

#endif  // WAVEFLOW_STREAMS_STREAM_DISPATCH_TASK_H
