// fir_sandbox.cpp — the 3-module DATAFLOW FIR kernel.
//
// Structure (load-compute-store dataflow over a resident, double-buffered row
// buffer) — the hardware realization of three concurrent SimObjs over channels:
//
//   fir_load_row --xbuf (PIPO, windowed BRAM)--> fir_compute_row --yfifo (FIFO)--> fir_store_row
//
// The DATAFLOW region is the body of the row loop, so each row streams through
// the three concurrent modules and Vitis double-buffers the inter-task channels
// (xbuf, yfifo) across row iterations (depth N_PINGPONG=2) — load(row r+1)
// overlaps compute(row r) overlaps store(row r-1).  This per-row form is the
// canonical Vitis dataflow idiom: it is correct under C-simulation's sequential
// semantics (each iteration uses logically fresh channels) *and* synthesizes to
// the overlapped pipeline.
//
// The sliding FIR window X[i, j+T-1-t] forces a resident, randomly-addressable
// row buffer (you cannot FIR a pure stream), so load-compute-store is a
// correctness necessity, not a nicety.  The load->compute channel is therefore
// a partitioned-BRAM ping-pong (PIPO); the compute->store channel is sequential
// and lowers to a FIFO — the BRAM-channel vs FIFO-channel distinction.
#include "fir_sandbox.hpp"

namespace fir_sandbox {

// --------------------------------------------------------------------------
// Load: contiguous burst read of one row of X into the row buffer, plus the T
// FIR taps into a small tap buffer.  Sequential addresses X[base..base+n_cols-1]
// => the m_axi burst analyzer coalesces the row read into AXI bursts (full
// bandwidth on the read stream).  Both xbuf and htbuf are inter-task channels
// declared in the dataflow body, so compute consumes them as proper per-row
// channels (no cross-region constant array — which RTL would mis-lower).
// --------------------------------------------------------------------------
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

// --------------------------------------------------------------------------
// Compute: BRAM-only FIR datapath at II=1.  The per-output accumulator is
// iteration-local, so there is no loop-carried dependency and the COMPUTE loop
// pipelines at II=1.  The T-tap window reads land in parallel because xbuf is
// cyclically partitioned (factor T).  Reads no external memory (taps and window
// both come from BRAM channels): throughput is set purely by the load/store
// streams, not by compute.
// --------------------------------------------------------------------------
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
            // Accumulate left-to-right in t, the same order the numpy golden
            // uses, so the float result is bit-identical (no tolerance).
            acc += htbuf[t] * xbuf[j + (T - 1) - t];
        }
        yfifo.write(acc);
    }
}

// --------------------------------------------------------------------------
// Store: contiguous burst write of one output row.  Sequential addresses =>
// the m_axi write burst analyzer coalesces this into AXI bursts.
// --------------------------------------------------------------------------
static void fir_store_row(real_t* Y, int obase, int out_len,
                          hls::stream<real_t>& yfifo) {
STORE:
    for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
        Y[obase + j] = yfifo.read();
    }
}

}  // namespace fir_sandbox

// --------------------------------------------------------------------------
// Top: the row loop whose body is the DATAFLOW region wiring the three modules.
//
//   xbuf   : 1-D inter-task array -> Vitis lowers to a depth-N_PINGPONG=2 PIPO
//            (the load->compute double buffer); stays a PIPO (not a FIFO)
//            because compute needs windowed random access into the row.
//   htbuf  : 1-D inter-task array -> PIPO (the T taps, re-read per row by the
//            load module so they reach compute as a proper channel; tiny and
//            off the per-element path).
//   yfifo  : hls::stream -> FIFO (sequential compute->store).
// --------------------------------------------------------------------------
void fir_accel(
    const fir_sandbox::real_t* X,
    fir_sandbox::real_t*       Y,
    const fir_sandbox::real_t* h,
    int n_rows,
    int n_cols) {
    using namespace fir_sandbox;

// Cosim sizes the m_axi memory model from these depths, so they must match the
// testbench arrays (X = n_rows*n_cols, Y = n_rows*out_len).  Override per cosim
// run via -D (the committed fixture and the sweep set exact values); the large
// default is only a csynth-time upper bound.
#ifndef WF_FIR_X_DEPTH
#define WF_FIR_X_DEPTH 65536
#endif
#ifndef WF_FIR_Y_DEPTH
#define WF_FIR_Y_DEPTH 65536
#endif
#ifdef WF_FIR_SPLIT_BUNDLE
    // Split topology: X and Y on separate m_axi bundles.  Measured cosim latency
    // is IDENTICAL to the single-bundle build (see results/cosim_sweep.json):
    // a single m_axi bundle is already full-duplex, so the split buys nothing
    // for a read+write pair.  Kept as the controlled comparison that proves it.
#pragma HLS INTERFACE m_axi port=X offset=slave bundle=gmemX max_read_burst_length=256 depth=WF_FIR_X_DEPTH
#pragma HLS INTERFACE m_axi port=Y offset=slave bundle=gmemY max_write_burst_length=256 depth=WF_FIR_Y_DEPTH
#pragma HLS INTERFACE m_axi port=h offset=slave bundle=gmemX depth=8
#else
    // Single shared topology: X, Y, h on one m_axi bundle.  A single bundle has
    // independent AR/R and AW/W channels (full-duplex), so the X-read and Y-write
    // streams do NOT contend -> this already hits the same throughput as the
    // split build.  (The #streams->II floor applies to *same-direction* streams
    // sharing the one read channel, e.g. VMAC's two interleaved reads -- not to a
    // read+write pair.  This corrects the plan's "single-port read+write = II=2".)
#pragma HLS INTERFACE m_axi port=X offset=slave bundle=gmem max_read_burst_length=256 depth=WF_FIR_X_DEPTH
#pragma HLS INTERFACE m_axi port=Y offset=slave bundle=gmem max_write_burst_length=256 depth=WF_FIR_Y_DEPTH
#pragma HLS INTERFACE m_axi port=h offset=slave bundle=gmem depth=8
#endif
#pragma HLS INTERFACE s_axilite port=n_rows
#pragma HLS INTERFACE s_axilite port=n_cols
#pragma HLS INTERFACE s_axilite port=return

ROW:
    for (int r = 0; r < n_rows; ++r) {
#pragma HLS DATAFLOW
        // Per-row scalars declared inside the dataflow body (canonical form).
        const int out_len = n_cols - T + 1;
        const int in_base = r * n_cols;
        const int o_base = r * out_len;
        real_t xbuf[NCOL_MAX];
        // Cyclic partition (factor T) so the T-tap window reads land in one cycle.
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
