// Static matrix-LT FIR top: a single full-duplex m_axi gmem bundle + AXI-stream
// control (the command on s_in, the response on m_out — like shared_mem), wrapping the
// hand-written fir_dataflow::fir_accel_core hook (= the Phase 1 sandbox kernel).  The
// command rides s_in as the serialized FIRCmd words (op, tx_id, x_off, h_off, y_off,
// n_rows, n_cols); *_off are element (float) offsets into gmem (X, Y, h are three
// logical regions of one bundle — Phase 1 proved single-bundle read+write is full-duplex).
#include "fir.hpp"

#ifndef WF_FIR_MEM_DEPTH
#define WF_FIR_MEM_DEPTH 65536
#endif

void fir(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out, real_t* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem max_read_burst_length=256 max_write_burst_length=256 depth=WF_FIR_MEM_DEPTH
#pragma HLS INTERFACE ap_ctrl_hs port=return
    // Read the FIRCmd off the control stream (one 32-bit word per field, in order).
    ap_uint<32> op     = s_in.read();
    ap_uint<32> tx_id  = s_in.read();
    int x_off  = (int)s_in.read();
    int h_off  = (int)s_in.read();
    int y_off  = (int)s_in.read();
    int n_rows = (int)s_in.read();
    int n_cols = (int)s_in.read();
    (void)op;
    fir_dataflow::fir_accel_core(gmem + x_off, gmem + y_off, gmem + h_off, n_rows, n_cols);
    // Response back to the host on the control stream (echo tx_id, status = 0).
    fir_dataflow::fir_respond(m_out, tx_id);
}
