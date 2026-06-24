// fir_dflow_sandbox.cpp — poly control shell + scoped fir_dataflow DATAFLOW body.
//
// See fir_dflow_sandbox.hpp for the de-risk goals (plans/fir-cleanup.md §1 Phase 1).
//
// Structure mirrors the Phase-2 division of ownership exactly:
//   * top fir() = the poly shell: while(1) { read header; END->return;
//     err = fir_dataflow(...); if (err) { halt regs; return; } }
//   * fir_dataflow() = a *function-scoped* { #pragma HLS DATAFLOW } region with
//     load / compute / store wired by internal streams; the error leaves via a
//     depth-1 status stream read AFTER the region (not a shared scalar).
//
// The three stages overlap WITHIN a matrix (across its rows): load reads X row
// r+1 from gmem while compute does row r (BRAM windowed FIR) while store writes
// row r-1 to gmem — load-read ∥ store-write on the *same* gmem bundle is the
// full-duplex case. The header read sits OUTSIDE the region, so back-to-back
// commands have a small per-command fill/drain seam (the per-command barrier).
#include "fir_dflow_sandbox.hpp"

namespace fir_dflow {

// --------------------------------------------------------------------------
// load: validate size, emit metadata, burst-read the T taps + each X row from
// gmem into streams. On a bad-size command it emits n_rows=0 (so compute/store
// run zero row-trips) and err=FIR_ERR_BAD_SIZE in the metadata — no gmem row
// reads happen, so an absurd n_cols can't overrun anything.
// --------------------------------------------------------------------------
static void load(const FirCmdHdr& cmd, const real_t* gmem,
                 hls::stream<real_t>& x_s, hls::stream<real_t>& taps_s,
                 hls::stream<FirMeta>& meta_s) {
    const int n_rows = (int)cmd.n_rows;
    const int n_cols = (int)cmd.n_cols;
    const bool ok = (n_cols >= T) && (n_cols <= NCOL_MAX) &&
                    (n_rows >= 1) && (n_rows <= MAX_ROWS);

    FirMeta m;
    m.n_rows = ok ? n_rows : 0;
    m.n_cols = n_cols;
    m.y_off  = (int)cmd.y_off;
    m.tx_id  = cmd.tx_id;
    m.err    = ok ? (ap_uint<8>)FIR_ERR_NONE : (ap_uint<8>)FIR_ERR_BAD_SIZE;
    meta_s.write(m);

    const int h_base = (int)cmd.h_off;
LOAD_TAPS:
    for (int t = 0; t < T; ++t) {
#pragma HLS PIPELINE II=1
        taps_s.write(gmem[h_base + t]);
    }

    const int x_base = (int)cmd.x_off;
    const int rows = m.n_rows;            // 0 on error -> no row reads
LOAD_ROWS:
    for (int r = 0; r < rows; ++r) {
    LOAD_ROW:
        for (int j = 0; j < n_cols; ++j) {
#pragma HLS PIPELINE II=1
            x_s.write(gmem[x_base + r * n_cols + j]);
        }
    }
}

// --------------------------------------------------------------------------
// compute: read T taps + metadata, then for each row buffer the n_cols inputs
// into a cyclically-partitioned BRAM row (the sliding window needs random
// access) and emit out_len = n_cols-T+1 outputs at II=1. BRAM-only datapath, no
// external memory, so throughput is set by the load/store streams. Tap order is
// left-to-right (matches the golden) so the float result is bit-exact.
// --------------------------------------------------------------------------
static void compute(hls::stream<real_t>& x_s, hls::stream<real_t>& taps_s,
                    hls::stream<FirMeta>& meta_in,
                    hls::stream<real_t>& y_s, hls::stream<FirMeta>& meta_out) {
    const FirMeta m = meta_in.read();

    real_t htbuf[T];
#pragma HLS ARRAY_PARTITION variable=htbuf complete dim=1
LOAD_TAPS_C:
    for (int t = 0; t < T; ++t) {
#pragma HLS PIPELINE II=1
        htbuf[t] = taps_s.read();
    }
    meta_out.write(m);

    const int n_cols  = m.n_cols;
    const int out_len = n_cols - T + 1;
    real_t xbuf[NCOL_MAX];
#pragma HLS ARRAY_PARTITION variable=xbuf cyclic factor=8 dim=1

COMPUTE_ROWS:
    for (int r = 0; r < m.n_rows; ++r) {
    READ_ROW:
        for (int j = 0; j < n_cols; ++j) {
#pragma HLS PIPELINE II=1
            xbuf[j] = x_s.read();
        }
    COMPUTE_OUT:
        for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
            real_t acc = 0;
        TAPS:
            for (int t = 0; t < T; ++t) {
#pragma HLS UNROLL
                acc += htbuf[t] * xbuf[j + (T - 1) - t];
            }
            y_s.write(acc);
        }
    }
}

// --------------------------------------------------------------------------
// store: read metadata, burst-write each output row to gmem, emit the response
// (tx_id, status) on m_out, and write EXACTLY ONE err to the depth-1 status
// stream — the single channel the error leaves the DATAFLOW region by.
// --------------------------------------------------------------------------
static void store(hls::stream<real_t>& y_s, hls::stream<FirMeta>& meta_in,
                  real_t* gmem, hls::stream<ap_uint<32>>& m_out,
                  hls::stream<ap_uint<8>>& err_s) {
    const FirMeta m = meta_in.read();
    const int n_cols  = m.n_cols;
    const int out_len = n_cols - T + 1;
    const int y_base  = m.y_off;

STORE_ROWS:
    for (int r = 0; r < m.n_rows; ++r) {
    STORE_ROW:
        for (int j = 0; j < out_len; ++j) {
#pragma HLS PIPELINE II=1
            gmem[y_base + r * out_len + j] = y_s.read();
        }
    }

    // Response back to the host (echo tx_id + completion status), like poly.
    m_out.write((ap_uint<32>)m.tx_id);
    m_out.write((ap_uint<32>)m.err);
    // The one and only err for this command (read after the region in fir_dataflow).
    err_s.write(m.err);
}

// --------------------------------------------------------------------------
// fir_dataflow: the hand-written hook. A function-scoped #pragma HLS DATAFLOW
// region of load/compute/store wired by internal streams; the err leaves via a
// depth-1 status stream declared OUTSIDE the region and read AFTER it.
// --------------------------------------------------------------------------
ap_uint<8> fir_dataflow(const FirCmdHdr& cmd, real_t* gmem,
                        hls::stream<ap_uint<32>>& m_out) {
    hls::stream<ap_uint<8>> err_s;
#pragma HLS STREAM variable=err_s depth=1
    {
#pragma HLS DATAFLOW
        // Inter-task channels declared inside the dataflow body (canonical form).
        hls::stream<real_t> ld2cp;
#pragma HLS STREAM variable=ld2cp depth=1024
        hls::stream<real_t> taps_s;
#pragma HLS STREAM variable=taps_s depth=16
        hls::stream<real_t> cp2st;
#pragma HLS STREAM variable=cp2st depth=1024
        hls::stream<FirMeta> ld_meta;
#pragma HLS STREAM variable=ld_meta depth=2
        hls::stream<FirMeta> cp_meta;
#pragma HLS STREAM variable=cp_meta depth=2

        load(cmd, gmem, ld2cp, taps_s, ld_meta);
        compute(ld2cp, taps_s, ld_meta, cp2st, cp_meta);
        store(cp2st, cp_meta, gmem, m_out, err_s);
    }
    return err_s.read();
}

}  // namespace fir_dflow

// --------------------------------------------------------------------------
// Top: the poly control shell. ap_start-gated while(1) command loop; header
// read here (so END/error -> return stay at the top); fir_dataflow runs the
// body; on a returned err set the halt registers and return (processor restarts
// by re-asserting ap_start).
// --------------------------------------------------------------------------
void fir(
    hls::stream<ap_uint<32>>& s_in,
    hls::stream<ap_uint<32>>& m_out,
    ap_uint<1>&  halted,
    ap_uint<8>&  error,
    ap_uint<16>& tx_id,
    fir_dflow::real_t* gmem
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE s_axilite port=halted bundle=control
#pragma HLS INTERFACE s_axilite port=error  bundle=control
#pragma HLS INTERFACE s_axilite port=tx_id  bundle=control
#pragma HLS INTERFACE m_axi    port=gmem offset=slave bundle=gmem \
    max_read_burst_length=256 max_write_burst_length=256 depth=WF_FIR_GMEM_DEPTH
#pragma HLS INTERFACE s_axilite port=gmem   bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control
    using namespace fir_dflow;

    // Initialize the status outputs at entry (each fresh ap_start is a fresh run).
    // halted/error are s_axilite OUTPUT regs the kernel only ever *sets* (to 1, on
    // error). poly leaves clearing them to the processor's pre-restart AXI write —
    // but an output reg can't be driven from the host side in C/RTL cosim, so it
    // reads stale (==1) after a clean restart. Driving them to 0 here makes the
    // status RTL-consistent across restarts (a Phase-2 requirement the cosim
    // surfaced; poly's shell omits this and its tests never restart-and-check).
    halted = 0;
    error  = 0;

    while (true) {
        FirCmdHdr cmd;
        cmd.read_stream(s_in);
        if (cmd.op == FIR_OP_END) {
            return;
        }
        ap_uint<8> err = fir_dataflow(cmd, gmem, m_out);
        if (err != (ap_uint<8>)FIR_ERR_NONE) {
            error  = err;
            tx_id  = (ap_uint<16>)cmd.tx_id;
            halted = 1;
            return;
        }
    }
}
