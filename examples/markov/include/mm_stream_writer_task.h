#ifndef WAVEFLOW_BUILD_MM_STREAM_WRITER_TASK_H
#define WAVEFLOW_BUILD_MM_STREAM_WRITER_TASK_H
// mm_stream_writer_task.h -- the FIXED bus-writer bodies of a routed credit stream
// (plans/mm_credit_stream.md, waveflow/hw/mm_credit.py::MmStreamWriter).  Two modes, two bodies:
//
//   mm_queue_writer_task   one producer write (TLAST-delimited, at most MAXP words) -> ONE queue-in
//                          packet [len | data...] burst to the queue-in view at `target`.  The
//                          producer's credit already proved the room, so the burst never stalls.
//   mm_credit_writer_task  one cumulative credit value -> one word at the credit-in view at `target`.
//                          Values that queued up behind a busy bus are coalesced to the newest (a
//                          bounded read_nb drain): cumulative, so nothing is lost.
//
// `target` is a WORD index on the bus (the m_axi pointer's base register is left at 0), driven by the
// system top as a stable input -- so the writer's RTL does not depend on where its peer is placed
// (plans/bus_address_map.md D6).  Single-firing bodies; the hls::task runtime re-fires them.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

template <int DW, int MAXP>
static void mm_queue_writer_task(hls::stream<streamutils::axi4s_word<DW> >& s_in,
                                 ap_uint<DW>* m_mem, ap_uint<32> target) {
    ap_uint<DW> buf[MAXP];
    int n = 0;
    bool last = false;
GATHER: do {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=MAXP
        const ap_uint<DW> d =
            streamutils::read_boundary_word<streamutils::axi4s_word<DW>, DW>(s_in, last);
        if (n < MAXP) buf[n++] = d;
    } while (!last);
BURST: for (int i = 0; i <= n; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=2 max=MAXP+1
        m_mem[target + i] = (i == 0) ? ap_uint<DW>(n) : buf[i - 1];
    }
}

template <int DW>
static void mm_credit_writer_task(hls::stream<ap_uint<DW> >& s_in, ap_uint<DW>* m_mem,
                                  ap_uint<32> target) {
    ap_uint<DW> v = s_in.read();
    ap_uint<DW> w;
COALESCE: for (int i = 0; i < 4; ++i) {
#pragma HLS PIPELINE II=1
        if (!s_in.read_nb(w)) break;
        v = w;
    }
    m_mem[target] = v;
}

#endif  // WAVEFLOW_BUILD_MM_STREAM_WRITER_TASK_H
