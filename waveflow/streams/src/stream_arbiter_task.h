#ifndef WAVEFLOW_STREAMS_STREAM_ARBITER_TASK_H
#define WAVEFLOW_STREAMS_STREAM_ARBITER_TASK_H
// stream_arbiter_task.h -- StreamArbiter's hls::task body (waveflow/streams/arbiter.py), framework.
//
// ONE FIRING = ONE FRAME, or nothing: test every input with empty(); if one holds a word, the first in
// the search order wins -- its index to m_sel (one word, TLAST), then its frame to m_out, whole: NB
// TLAST bursts, word by word.  No input holding a word: the firing does nothing, and the task re-fires.
//
// Template parameters, in the order the composite generator supplies them (KernelTask: template_args,
// then one word type per `framed` port, in signature order):
//   W    the data width of every port;
//   NB   TLAST bursts a frame (1: a message; 2: a header, then its payload; ...);
//   RR   0: strict priority, input 0 first; 1: round-robin, the search starts one past the last winner;
//   TO   m_out's word type, TS m_sel's, TI... each input's -- streamutils::axi4s_word<W> at a top's port,
//        streamutils::framed_word<W> on an internal FIFO.
//
// The inputs are the pack, so they come last in the signature (m_out, m_sel, s_in_0, ...); their number
// is the pack's length.  The test is empty() on each input, so the firing never blocks on an input that
// may stay empty -- and commits on a frame's first word: an input announcing a frame still being
// produced stalls the others until it is done (the block's first rule).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

namespace stream_arbiter_body {

// Bit K.. of `ready`: whether each input from K on holds a word.
template <int K>
static inline void ready_bits(ap_uint<16>&) {
#pragma HLS INLINE
}

template <int K, typename T0, typename... TR>
static inline void ready_bits(ap_uint<16>& ready, hls::stream<T0>& s0, hls::stream<TR>&... rest) {
#pragma HLS INLINE
    ready[K] = !s0.empty();
    ready_bits<K + 1>(ready, rest...);
}

// One frame, NB TLAST bursts, from s to d: a word a cycle.
//
// Keep this loop's shape: the count of TLASTs seen, tested at the head (`while (b < NB)`), a narrow
// count (NB <= 255; a 32-bit one was the critical path).  Two rewrites meant to shorten the path passed
// pysim and failed at RTL (Vitis 2025.1, plans/shared_kernel.md Stage 2): a `for (;;)` with a `break`
// after the read, and a `done` flag computed a burst ahead -- each hung the task after an input's last
// frame, a read issued past the end of the frame.
template <int W, int NB, typename TS, typename TD>
static void copy_frame(hls::stream<TS>& s, hls::stream<TD>& d) {
    ap_uint<8> b = 0;
COPY:
    while (b < NB) {
#pragma HLS PIPELINE II=1
        const TS x = s.read();
        streamutils::write_boundary_word<TD, W>(d, ap_uint<W>(x.data), bool(x.last));
        if (x.last) ++b;
    }
}

// Input `k`'s frame to d: the pack walked to its k-th stream.
template <int W, int NB, int K, typename TD>
static inline void copy_from(int, hls::stream<TD>&) {
#pragma HLS INLINE
}

template <int W, int NB, int K, typename TD, typename T0, typename... TR>
static inline void copy_from(int k, hls::stream<TD>& d, hls::stream<T0>& s0, hls::stream<TR>&... rest) {
#pragma HLS INLINE
    if (k == K) {
        copy_frame<W, NB>(s0, d);
    } else {
        copy_from<W, NB, K + 1>(k, d, rest...);
    }
}

}  // namespace stream_arbiter_body

template <int W, int NB, int RR, typename TO, typename TS, typename... TI>
static void stream_arbiter_task(hls::stream<TO>& m_out, hls::stream<TS>& m_sel, hls::stream<TI>&... s_in) {
    constexpr int N = sizeof...(TI);
    static_assert(N >= 2 && N <= 16, "stream_arbiter_task: 2 to 16 inputs");
    static_assert(NB >= 1, "stream_arbiter_task: a frame is at least one burst");
    // Round-robin: where the next search starts.  Reset with the task (a write-first static).
    static ap_uint<5> next_k = 0;
#pragma HLS reset variable=next_k

    ap_uint<16> ready = 0;
    stream_arbiter_body::ready_bits<0>(ready, s_in...);
    if (ready == 0) return;

    int win = 0;
    bool found = false;
SEARCH:
    for (int j = 0; j < N; ++j) {
#pragma HLS UNROLL
        int k = RR ? int(next_k) + j : j;
        if (k >= N) k -= N;
        if (!found && ready[k]) {
            win = k;
            found = true;
        }
    }
    if (RR) next_k = (win + 1 == N) ? 0 : win + 1;

    streamutils::write_boundary_word<TS, W>(m_sel, ap_uint<W>(win), true);
    stream_arbiter_body::copy_from<W, NB, 0>(win, m_out, s_in...);
}

#endif  // WAVEFLOW_STREAMS_STREAM_ARBITER_TASK_H
