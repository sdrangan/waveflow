// fir_dataflow.tpp — the hand-written load-compute-store DATAFLOW FIR kernel,
// reused verbatim as the @synthesizable(impl_file="fir_dataflow.tpp") hook for the
// matrix-LT FIR accelerator (examples/rowwise_fir/fir.py).
//
// This IS the validated Phase 1 sandbox kernel (examples/rowwise_fir/sandbox/
// fir_sandbox.cpp), refactored so the m_axi / s_axilite *interface* pragmas live
// in the generated top (fir.cpp) and the kernel body is a reusable core
// `fir_dataflow::fir_accel_core(X, Y, h, n_rows, n_cols)`.  Everything timing-
// relevant — the per-row #pragma HLS DATAFLOW over a partitioned-BRAM ping-pong
// (windowed) + a FIFO, compute II=1, the inferred X-read / Y-write bursts — is
// unchanged from Phase 1 (results/fir_sandbox_results.md).
#pragma once

#include <hls_stream.h>

namespace fir_dataflow {

typedef float real_t;                 // IEEE-754 single; golden is bit-exact
static const int T          = 8;      // FIR taps
static const int NCOL_MAX   = 1024;   // BRAM row-buffer depth bound
static const int N_PINGPONG = 2;      // load->compute double buffer (PIPO depth)

// --- load: burst-read one row of X + the T taps into BRAM channels -----------
static void fir_load_row(const real_t* X, const real_t* h, int base, int n_cols,
                         real_t xbuf[NCOL_MAX], real_t htbuf[T]) {
LOAD_TAPS:
    for (int t = 0; t < T; ++t) {
#pragma HLS PIPELINE II=1
        htbuf[t] = h[t];
    }
LOAD:
    for (int j = 0; j < n_cols; ++j) {
#pragma HLS PIPELINE II=1
        xbuf[j] = X[base + j];
    }
}

// --- compute: BRAM-only FIR datapath at II=1 (no external memory) ------------
static void fir_compute_row(const real_t htbuf[T], real_t xbuf[NCOL_MAX],
                            int n_cols, hls::stream<real_t>& yfifo) {
    const int out_len = n_cols - T + 1;
COMPUTE:
    for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
        real_t acc = 0;
    TAPS:
        for (int t = 0; t < T; ++t) {
#pragma HLS UNROLL
            // Left-to-right in t — the same order fir_golden accumulates, so the
            // float result is bit-identical (no tolerance).
            acc += htbuf[t] * xbuf[j + (T - 1) - t];
        }
        yfifo.write(acc);
    }
}

// --- store: burst-write one output row ---------------------------------------
static void fir_store_row(real_t* Y, int obase, int out_len,
                          hls::stream<real_t>& yfifo) {
STORE:
    for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
        Y[obase + j] = yfifo.read();
    }
}

// --- core: the row loop whose body is the DATAFLOW region --------------------
// Interface pragmas (m_axi / s_axilite) belong to the generated top (fir.cpp);
// this is the pure kernel, callable with any pointers (the generated top passes
// gmem + element offsets — a single full-duplex bundle, validated in Phase 1).
static void fir_accel_core(const real_t* X, real_t* Y, const real_t* h,
                           int n_rows, int n_cols) {
    const int out_len = n_cols - T + 1;
ROW:
    for (int r = 0; r < n_rows; ++r) {
#pragma HLS DATAFLOW
        const int in_base = r * n_cols;
        const int o_base = r * out_len;
        real_t xbuf[NCOL_MAX];
#pragma HLS ARRAY_PARTITION variable=xbuf cyclic factor=8 dim=1
        real_t htbuf[T];
#pragma HLS ARRAY_PARTITION variable=htbuf complete dim=1
        hls::stream<real_t> yfifo;
#pragma HLS STREAM variable=yfifo depth=64

        fir_load_row(X, h, in_base, n_cols, xbuf, htbuf);
        fir_compute_row(htbuf, xbuf, n_cols, yfifo);
        fir_store_row(Y, o_base, out_len, yfifo);
    }
}

}  // namespace fir_dataflow
