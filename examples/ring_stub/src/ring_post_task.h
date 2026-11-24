#ifndef EXAMPLES_RING_STUB_POST_TASK_H
#define EXAMPLES_RING_STUB_POST_TASK_H
// ring_post_task.h -- the ring stub's post stage (HAND-WRITTEN), the HLS twin of
// examples/ring_stub/ring_stub.py::Post.  ONE FIRING = ONE FRAME out of K:
//
//   1. its tag (s_tag, beside K: tag i belongs to frame i, since K keeps order), then its n words;
//   2. npass 0: back to the mux on m_fb -- the tag (npass 1), then the words.  The whole frame is
//      read before any of it is written (store and forward), so the mux never commits to a frame
//      that is still arriving;
//   3. npass 1: the answer -- Resp, the words, Status -- then a credit token on m_crd.
//
// Post never blocks on m_fb or m_crd: the mux's credit bounds what is in the loop, and those links
// hold it (ring_stub.py, DEPTH).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "uint64_array_utils.h"
#include "tag.h"
#include "crd.h"
#include "resp.h"
#include "status.h"

#ifndef RING_MAXN
#define RING_MAXN 64
#endif

template <int W, typename TT, typename TK, typename TF, typename TO, typename TS, typename TC>
static void ring_post_task(hls::stream<TT>& s_tag, hls::stream<TK>& s_k, hls::stream<TF>& m_fb,
                           hls::stream<TO>& m_out, hls::stream<TS>& m_status, hls::stream<TC>& m_crd) {
    static_assert(W == 64, "ring_post_task: one 64-bit element a word");

    // 1. the tag, then the whole frame
    Tag t;
    streamutils::read_msg<W>(t, s_tag);
    const int n = t.n;
    uint64_array_utils::value_type buf[RING_MAXN];
READ:
    for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
        uint64_array_utils::value_type v[1];
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        uint64_array_utils::read_boundary_lane<W>(s_k, v, 1, ts);
        buf[i] = v[0];
    }

    // 2. the first pass: back to the mux
    if (t.npass == 0) {
        Tag f;
        f.tx_id = t.tx_id;
        f.n = t.n;
        f.npass = 1;
        streamutils::write_msg<W>(f, m_fb);
    FB:
        for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
            uint64_array_utils::value_type v[1];
            v[0] = buf[i];
            uint64_array_utils::write_boundary_lane<W>(v, m_fb, i == n - 1, 1);
        }
        return;
    }

    // 3. the second pass: the answer, then the credit
    Resp r;
    r.tx_id = t.tx_id;
    r.n = t.n;
    streamutils::write_msg<W>(r, m_out);
OUT:
    for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
        uint64_array_utils::value_type v[1];
        v[0] = buf[i];
        uint64_array_utils::write_boundary_lane<W>(v, m_out, i == n - 1, 1);
    }
    Status s;
    s.tx_id = t.tx_id;
    s.status = StatusCode::OK;
    streamutils::write_msg<W>(s, m_status);
    Crd c;
    c.tx_id = t.tx_id;
    streamutils::write_msg<W>(c, m_crd);
}

#endif  // EXAMPLES_RING_STUB_POST_TASK_H
