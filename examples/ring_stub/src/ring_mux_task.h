#ifndef EXAMPLES_RING_STUB_MUX_TASK_H
#define EXAMPLES_RING_STUB_MUX_TASK_H
// ring_mux_task.h -- the ring stub's mux (HAND-WRITTEN), the HLS twin of
// examples/ring_stub/ring_stub.py::Mux.  The only way into K.  ONE FIRING:
//
//   1. absorb a credit token, if one is waiting (s_crd: a job has left the loop);
//   2. FEEDBACK FIRST: if a feedback frame is waiting (s_fb), forward its tag to m_tag and its words
//      to m_k -- a whole frame.  Post writes a feedback frame only once it has all of it, so this copy
//      never waits on a frame still being produced;
//   3. else, a new job (s_in) if a credit is held: its tag (npass 0) to m_tag, its words to m_k.
//
// Every test is empty(): the firing never blocks on an input that may stay empty, and a frame is
// read only once its head is there.  The credit bounds the jobs in the loop to FMAX, and every link
// in the loop holds what FMAX jobs can put there, so Post never blocks writing m_fb or m_crd.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "uint64_array_utils.h"
#include "job.h"
#include "tag.h"
#include "crd.h"

// n words from s to d, TLAST on the last: one element a word (W = 64).
template <int W, typename TS, typename TD>
static void ring_copy_frame(hls::stream<TS>& s, hls::stream<TD>& d, int n) {
COPY:
    for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
        uint64_array_utils::value_type v[1];
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        uint64_array_utils::read_boundary_lane<W>(s, v, 1, ts);
        uint64_array_utils::write_boundary_lane<W>(v, d, i == n - 1, 1);
    }
}

template <int W, int FMAX, typename TI, typename TF, typename TC, typename TK, typename TT>
static void ring_mux_task(hls::stream<TI>& s_in, hls::stream<TF>& s_fb, hls::stream<TC>& s_crd,
                          hls::stream<TK>& m_k, hls::stream<TT>& m_tag) {
    static_assert(W == 64, "ring_mux_task: one 64-bit element a word");
    static ap_uint<8> credit = FMAX;
#pragma HLS reset variable=credit

    // 1. a credit back
    if (!s_crd.empty()) {
        Crd c;
        streamutils::read_msg<W>(c, s_crd);
        credit = credit + 1;
    }

    // 2. feedback first, a whole frame
    if (!s_fb.empty()) {
        Tag t;
        streamutils::read_msg<W>(t, s_fb);
        streamutils::write_msg<W>(t, m_tag);
        ring_copy_frame<W>(s_fb, m_k, t.n);
        return;
    }

    // 3. a new job, with a credit
    if (credit > 0 && !s_in.empty()) {
        Job j;
        streamutils::read_msg<W>(j, s_in);
        credit = credit - 1;
        Tag t;
        t.tx_id = j.tx_id;
        t.n = j.n;
        t.npass = 0;
        streamutils::write_msg<W>(t, m_tag);
        ring_copy_frame<W>(s_in, m_k, j.n);
    }
}

#endif  // EXAMPLES_RING_STUB_MUX_TASK_H
