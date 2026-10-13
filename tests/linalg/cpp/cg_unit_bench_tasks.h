// cg_unit_bench_tasks.h -- TEST ONLY: the framers that feed a CgVectorUnit from memory through the
// in-band MemRStream and land its replies through the in-band MemWStream.
//
// cg_req_task:   one CgBenchReq (where the request's payload is, in words) and the request's
//                LinalgHeader, both plain on s_req -> MemRCmd(payload, fwd 1) | header.
// cg_reply_task: one reply -> MemWCmd(tag, length, fwd 1) | header | the payload: the test uses the
//                tag as the reply's word address; MemWStream echoes the header on s_done.  The
//                reader and the writer fire once per message each, which the generated top's m_axi
//                pointer FIFOs require (their firing counts are coupled).
// Python twins: tests/linalg/_cg_unit_bench.py (CgReqFramer, CgReplyFramer).
#ifndef WF_TEST_CG_UNIT_BENCH_TASKS_H
#define WF_TEST_CG_UNIT_BENCH_TASKS_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_r_cmd.h"
#include "mem_w_cmd.h"
#include "cg_bench_req.h"
#include "wf_linalg_header.h"

template <int WBW>
static void cg_req_task(hls::stream<ap_uint<WBW> >& s_req,
                        hls::stream<streamutils::framed_word<WBW> >& cmd_out) {
    CgBenchReq q;
    q.template read_stream<WBW>(s_req);
    LinalgHeader h;
    h.template read_stream<WBW>(s_req);
    MemRCmd r;
    r.addr = q.off;
    r.len = q.len;
    r.fwd_bursts = 1;
    r.template write_framed_stream<WBW>(cmd_out);
    h.template write_framed_stream<WBW>(cmd_out);
}

template <int WBW>
static void cg_reply_task(hls::stream<streamutils::framed_word<WBW> >& s_in,
                          hls::stream<streamutils::framed_word<WBW> >& s_out) {
    LinalgHeader h;
    streamutils::tlast_status tl;
    h.template read_framed_stream<WBW>(s_in, tl);
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

#endif  // WF_TEST_CG_UNIT_BENCH_TASKS_H
