// fir_respond_impl.tpp — hand-written response hook for the rowwise_fir example.
//
// Mirrors shared_mem's hist_respond_impl: when a FIR command completes, emit a small
// response on the m_out AXI4-Stream — the transaction id echo + a status word.  Kept as
// a separate hook file (like hist_respond_impl.tpp) so the static top reads cleanly and
// the response path is a named, hand-written unit.
#pragma once

#include <hls_stream.h>
#include <ap_int.h>

namespace fir_dataflow {

inline void fir_respond(hls::stream<ap_uint<32> >& m_out, ap_uint<32> tx_id) {
    m_out.write(tx_id);            // transaction id echo
    m_out.write(ap_uint<32>(0));   // status = 0 (ok)
}

}  // namespace fir_dataflow
