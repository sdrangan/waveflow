// unit_bench_tasks.h -- TEST ONLY: the framers that feed a SystolicUnit from memory through the
// in-band MemRStream and land its replies through the in-band MemWStream.
//
// unit_req_task:   one UnitBenchReq (where A and B are, in words) and the request's LinalgHeader,
//                  both plain on s_req -> MemRCmd(A, fwd 1) | header | MemRCmd(B, fwd 0).
// unit_reply_task: one reply -> MemWCmd(0, 0, fwd 0) | MemWCmd(tag, length, fwd 1) | header | the
//                  payload: the test uses the tag as the reply's word address; MemWStream echoes
//                  the header on s_done.  The first, empty write balances the job's two reads: in
//                  the generated top the m_axi pointer FIFOs couple the reader's and the writer's
//                  firing counts, and a writer that fires less than the reader stalls the design
//                  once its pointer FIFO is full (depth 9: after eight jobs).
// Python twins: tests/linalg/_unit_bench.py (UnitReqFramer, UnitReplyFramer).
#ifndef WF_TEST_UNIT_BENCH_TASKS_H
#define WF_TEST_UNIT_BENCH_TASKS_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_r_cmd.h"
#include "mem_w_cmd.h"
#include "unit_bench_req.h"
#include "wf_linalg_header.h"

template <int WBW>
static void unit_req_task(hls::stream<ap_uint<WBW> >& s_req,
                          hls::stream<streamutils::framed_word<WBW> >& cmd_out) {
    UnitBenchReq q;
    q.template read_stream<WBW>(s_req);
    LinalgHeader h;
    h.template read_stream<WBW>(s_req);
    MemRCmd ra;
    ra.addr = q.a_off;
    ra.len = q.a_len;
    ra.fwd_bursts = 1;
    ra.template write_framed_stream<WBW>(cmd_out);
    h.template write_framed_stream<WBW>(cmd_out);
    MemRCmd rb;
    rb.addr = q.b_off;
    rb.len = q.b_len;
    rb.fwd_bursts = 0;
    rb.template write_framed_stream<WBW>(cmd_out);
}

template <int WBW>
static void unit_reply_task(hls::stream<streamutils::framed_word<WBW> >& s_in,
                            hls::stream<streamutils::framed_word<WBW> >& s_out) {
    LinalgHeader h;
    streamutils::tlast_status tl;
    h.template read_framed_stream<WBW>(s_in, tl);
    MemWCmd e;
    e.addr = 0;
    e.len = 0;
    e.fwd_bursts = 0;
    e.template write_framed_stream<WBW>(s_out);
    MemWCmd w;
    w.addr = h.tag;
    w.len = h.length;
    w.fwd_bursts = 1;
    w.template write_framed_stream<WBW>(s_out);
    h.template write_framed_stream<WBW>(s_out);
PAYLOAD:
    for (ap_uint<32> i = 0; i < h.length; ++i) {
#pragma HLS PIPELINE II = 1
        s_out.write(s_in.read());
    }
}

#endif  // WF_TEST_UNIT_BENCH_TASKS_H
