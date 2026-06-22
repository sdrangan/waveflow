// Generated matrix-LT FIR top (hand-rolled, VMAC render_top style): a single
// full-duplex m_axi gmem bundle + s_axilite scalar command, wrapping the
// hand-written fir_dataflow::fir_accel_core hook (= the Phase 1 sandbox kernel).
#include "fir.hpp"

#ifndef WF_FIR_MEM_DEPTH
#define WF_FIR_MEM_DEPTH 65536
#endif

void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols) {
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem max_read_burst_length=256 max_write_burst_length=256 depth=WF_FIR_MEM_DEPTH
#pragma HLS INTERFACE s_axilite port=x_off
#pragma HLS INTERFACE s_axilite port=y_off
#pragma HLS INTERFACE s_axilite port=h_off
#pragma HLS INTERFACE s_axilite port=n_rows
#pragma HLS INTERFACE s_axilite port=n_cols
#pragma HLS INTERFACE s_axilite port=return
    fir_dataflow::fir_accel_core(gmem + x_off, gmem + y_off, gmem + h_off,
                                 n_rows, n_cols);
}
