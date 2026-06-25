// fir_freerun_sandbox.cpp — free-running streaming FIR (see fir_freerun_sandbox.hpp).
//
// Three persistent DATAFLOW processes over plain hls::stream FIFOs:
//
//   load  --ld_ctrl(Cmd)/ld_data(float: h then X)-->  compute
//   compute --cp_ctrl(Cmd)/cp_data(float: Y)-->       store
//
// Each is a while(!done) loop draining on the END sentinel.  Jobs pipeline across
// the network: load(N+1)'s X-read burst overlaps store(N)'s Y-write burst on the
// shared gmem bundle (full-duplex AR/R vs AW/W) — the inter-job overlap the
// control-driven per-job-barrier kernel structurally lacked.
#include "fir_freerun_sandbox.hpp"

namespace fir_freerun {

// --------------------------------------------------------------------------
// load: read each command in-band off s_in; validate its size; forward the Cmd
// on ld_ctrl; for a good job, read h (T taps, cached per command in compute) and
// the X samples from gmem via read_array_slice (CHUNK-sized AXI bursts) and stream
// them as float on ld_data.  A bad-size job forwards the Cmd with ST_BAD_SIZE and
// streams NO data (compute/store skip it) — the pipeline keeps flowing.
// --------------------------------------------------------------------------
static void load(hls::stream<word_t>& s_in, const ap_uint<MEM_DW>* gmem,
                 hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data) {
    bool done = false;
LOAD_CMDS: while (!done) {
        Cmd c;
        c.op     = (ap_uint<2>)(unsigned)s_in.read();
        c.tx_id  = (ap_uint<16>)(unsigned)s_in.read();
        c.x_off  = (int)s_in.read();
        c.h_off  = (int)s_in.read();
        c.y_off  = (int)s_in.read();
        c.n_rows = (int)s_in.read();
        c.n_cols = (int)s_in.read();
        if (c.op == OP_END) { c.status = (ap_uint<8>)ST_OK; ld_ctrl.write(c); done = true; break; }

        const bool ok = (c.n_cols >= T && c.n_cols <= NCOL_MAX &&
                         c.n_rows >= 1 && c.n_rows <= MAX_ROWS);
        c.status = ok ? (ap_uint<8>)ST_OK : (ap_uint<8>)ST_BAD_SIZE;
        ld_ctrl.write(c);
        if (!ok) continue;                       // bad job: no data streamed

        // h (T taps), read once per command -> compute caches it in tap registers.
        float hb[T];
        read_array_slice<MEM_DW>(gmem, c.h_off, c.h_off + T, hb);
        for (int t = 0; t < T; ++t) ld_data.write(hb[t]);

        // X in CHUNK-sized bursts -> streamed sample-by-sample to compute.
        const int total = c.n_rows * c.n_cols;
        float cb[CHUNK];
    LOAD_X: for (int base = 0; base < total; base += CHUNK) {
            const int k = (total - base < CHUNK) ? (total - base) : CHUNK;
            read_array_slice<MEM_DW>(gmem, c.x_off + base, c.x_off + base + k, cb);
            for (int i = 0; i < k; ++i) ld_data.write(cb[i]);
        }
    }
}

// --------------------------------------------------------------------------
// compute: streaming shift-register FIR.  Cache the T taps per command; for each
// row, flush a T-deep fully-partitioned tapped delay line, read n_cols samples,
// and emit n_cols-T+1 outputs at II=1 (the first T-1 samples fill the window and
// produce no output — the "valid" edge).  Tap order t=0..T-1 with sr[t]=x[s-t]
// reproduces fir_golden's left-to-right accumulation -> bit-exact.
// --------------------------------------------------------------------------
static void compute(hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data,
                    hls::stream<Cmd>& cp_ctrl, hls::stream<float>& cp_data) {
    bool done = false;
COMPUTE_CMDS: while (!done) {
        Cmd c = ld_ctrl.read();
        if (c.op == OP_END) { cp_ctrl.write(c); done = true; break; }
        cp_ctrl.write(c);
        if (c.status != (ap_uint<8>)ST_OK) continue;   // bad job: no data

        float h[T];
#pragma HLS ARRAY_PARTITION variable=h complete dim=1
        for (int t = 0; t < T; ++t) h[t] = ld_data.read();

    ROWS: for (int r = 0; r < c.n_rows; ++r) {
            float sr[T];                                // tapped delay line: sr[t] = x[s-t]
#pragma HLS ARRAY_PARTITION variable=sr complete dim=1
            for (int k = 0; k < T; ++k) sr[k] = 0.0f;   // per-row window flush
        COLS: for (int s = 0; s < c.n_cols; ++s) {
#pragma HLS PIPELINE II=1
                const float x = ld_data.read();
                for (int k = T - 1; k >= 1; --k) {
#pragma HLS UNROLL
                    sr[k] = sr[k - 1];
                }
                sr[0] = x;
                if (s >= T - 1) {                       // window full -> emit one output
                    float acc = 0.0f;
                    for (int t = 0; t < T; ++t) {
#pragma HLS UNROLL
                        acc += h[t] * sr[t];            // left-to-right -> bit-exact golden
                    }
                    cp_data.write(acc);
                }
            }
        }
    }
}

// --------------------------------------------------------------------------
// store: drain Y from cp_data, write it to gmem via write_array_slice (CHUNK-sized
// AXI bursts), and emit one resp{tx_id, status} per job on m_out.  A bad-size job
// writes no Y and emits status=ST_BAD_SIZE — the pipeline keeps flowing (no halt).
// --------------------------------------------------------------------------
static void store(hls::stream<Cmd>& cp_ctrl, hls::stream<float>& cp_data,
                  ap_uint<MEM_DW>* gmem, hls::stream<word_t>& m_out) {
    bool done = false;
STORE_CMDS: while (!done) {
        Cmd c = cp_ctrl.read();
        if (c.op == OP_END) { done = true; break; }     // END carries no response

        if (c.status == (ap_uint<8>)ST_OK) {
            const int out_len = c.n_cols - T + 1;
            const int total = c.n_rows * out_len;
            float cb[CHUNK];
        STORE_Y: for (int base = 0; base < total; base += CHUNK) {
                const int k = (total - base < CHUNK) ? (total - base) : CHUNK;
                for (int i = 0; i < k; ++i) cb[i] = cp_data.read();
                write_array_slice<MEM_DW>(cb, gmem, c.y_off + base, c.y_off + base + k);
            }
        }
        m_out.write((word_t)c.tx_id);
        m_out.write((word_t)(unsigned)c.status);
    }
}

}  // namespace fir_freerun

// --------------------------------------------------------------------------
// Top: ap_ctrl_hs + a single #pragma HLS DATAFLOW region of the three persistent
// processes.  One ap_start drives a whole batch (jobs + END); the END sentinel
// drains the network, the region completes, the top returns -> ap_done; the host
// re-asserts ap_start for the next batch (restart).
// --------------------------------------------------------------------------
void fir(hls::stream<fir_freerun::word_t>& s_in,
         hls::stream<fir_freerun::word_t>& m_out,
         ap_uint<fir_freerun::MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem \
    max_read_burst_length=256 max_write_burst_length=256 depth=WF_FIR_FR_GMEM_DEPTH
#pragma HLS INTERFACE ap_ctrl_hs port=return
    using namespace fir_freerun;
#pragma HLS DATAFLOW
    // Inter-task FIFOs.  ld_data / cp_data are sized to hold ~a job's worth of
    // floats so load can run a job ahead of store (the inter-job overlap); the
    // ctrl FIFOs only need to hold the in-flight job count.
    hls::stream<Cmd>   ld_ctrl;
#pragma HLS STREAM variable=ld_ctrl depth=8
    hls::stream<float> ld_data;
#pragma HLS STREAM variable=ld_data depth=1024
    hls::stream<Cmd>   cp_ctrl;
#pragma HLS STREAM variable=cp_ctrl depth=8
    hls::stream<float> cp_data;
#pragma HLS STREAM variable=cp_data depth=1024

    load(s_in, gmem, ld_ctrl, ld_data);
    compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    store(cp_ctrl, cp_data, gmem, m_out);
}
