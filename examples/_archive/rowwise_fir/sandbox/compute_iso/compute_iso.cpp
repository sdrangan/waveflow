// compute_iso.cpp — Rung 1 of the FIR-overlap ladder: the streaming FIR compute in ISOLATION.
//
// Same shift-register FIR as the freerun sandbox's compute() stage, but X comes from on-chip
// BRAM (not the load FIFO / m_axi) and Y is folded into an integer checksum (not the store FIFO /
// m_axi). So there is NO load and NO store — the cosim transaction latency (cmd in -> resp out)
// IS the pure compute time. We then fit:
//
//     compute_time = L0 + L1*nrow + II*(nrow*ncol)
//
// Goal: show the inner COLS loop achieves II = 1 in COSIM (not just csynth), with a small per-row
// term L1 (the window flush) and a fixed L0 (the ~47 FP-pipeline fill + cmd/resp handshake). If
// II != 1 here, the compute itself is the problem; if II = 1, the ~2.9 cyc/sample seen in the full
// FIR is a stage-overlap / bus issue, not compute.
#include <ap_int.h>
#include <cstdint>
#include <hls_stream.h>

#define MEM_DW 32
static const int T = 8;
static const int MAXE = 1024;          // max nrow*ncol on the sweep grid (e.g. 4x256, 1x1024)

static ap_uint<32> f2u(float f) { union { uint32_t i; float f; } c; c.f = f; return ap_uint<32>(c.i); }

// X is a BRAM (ap_memory) port the testbench fills — on-chip (no m_axi, so compute is isolated),
// but a RUNTIME input so csynth cannot constant-fold the reads (which would DCE the whole loop).
void compute_iso(const float xbuf[MAXE],
                 hls::stream<ap_uint<MEM_DW> >& cmd_in,
                 hls::stream<ap_uint<MEM_DW> >& resp_out) {
#pragma HLS INTERFACE ap_memory port=xbuf
#pragma HLS INTERFACE axis port=cmd_in
#pragma HLS INTERFACE axis port=resp_out
#pragma HLS INTERFACE ap_ctrl_hs port=return
    const int nrow = (int)cmd_in.read();
    const int ncol = (int)cmd_in.read();

    float h[T];
#pragma HLS ARRAY_PARTITION variable=h complete
    for (int t = 0; t < T; ++t) h[t] = (float)((t + 1) * 0.25f - 1.0f);

    ap_uint<32> chk = 0;
    ROWS: for (int r = 0; r < nrow; ++r) {
        float sr[T];                                  // tapped delay line sr[t] = x[s-t]
#pragma HLS ARRAY_PARTITION variable=sr complete
        for (int k = 0; k < T; ++k) sr[k] = 0.0f;     // per-row window flush
        COLS: for (int s = 0; s < ncol; ++s) {
#pragma HLS PIPELINE II=1
            float x = xbuf[r * ncol + s];
            for (int k = T - 1; k >= 1; --k) sr[k] = sr[k - 1];
            sr[0] = x;
            if (s >= T - 1) {
                float acc = 0.0f;
                for (int t = 0; t < T; ++t) acc += h[t] * sr[t];
                chk ^= f2u(acc);                      // integer XOR: 1-cyc recurrence, keeps II=1
            }
        }
    }
    resp_out.write(chk);
}
