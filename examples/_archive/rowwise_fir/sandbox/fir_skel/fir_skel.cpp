// fir_skel.cpp — Rung 4: the REAL shift-register FIR compute in the controlled load/store skeleton.
//
// Identical free-running skeleton to Rung 2/3 (ap_ctrl_hs + DATAFLOW + while(!done) + one gmem
// bundle + deep FIFOs), but the middle stage is now the ACTUAL FIR: per row, stream n_cols input
// samples through a T-tap shift register and emit n_cols-T+1 outputs (the first T-1 samples fill
// the window and produce no output). Two features vs the Rung-3 pass-through:
//   (a) read != write rate:  reads n_cols/row, writes (n_cols-T+1)/row
//   (b) per-row window flush + nested ROWS/COLS loop
// Taps h are COMPILE-TIME constants (no h-read) — that simplification is already applied, so if the
// overlap breaks here it is NOT the h-read. Job = (n_rows, n_cols). Measures period vs max/sum.
#include <ap_int.h>
#include <hls_stream.h>

#define MEM_DW 32
static const int T = 8;
static const int CHUNK = 256;
static const unsigned END_N = 0xFFFFFFFFu;
#ifndef LSDEPTH
#define LSDEPTH 2048
#endif

namespace {
struct Cmd { int nr; int nc; unsigned xoff; unsigned yoff; bool done; };

float u2f(ap_uint<MEM_DW> u) { union { unsigned i; float f; } c; c.i = (unsigned)u; return c.f; }
ap_uint<MEM_DW> f2u(float f) { union { unsigned i; float f; } c; c.f = f; return ap_uint<MEM_DW>(c.i); }

void fs_load(hls::stream<ap_uint<32> >& s_in, const ap_uint<MEM_DW>* gmem,
             hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned nr = (unsigned)s_in.read();
        if (nr == END_N) { Cmd c; c.nr = 0; c.nc = 0; c.xoff = 0; c.yoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned nc = (unsigned)s_in.read(), xoff = (unsigned)s_in.read(), yoff = (unsigned)s_in.read();
        Cmd c; c.nr = (int)nr; c.nc = (int)nc; c.xoff = xoff; c.yoff = yoff; c.done = false;
        ld_ctrl.write(c);
        int total = (int)nr * (int)nc;
        LX: for (int base = 0; base < total; base += CHUNK) {
            int k = (total - base < CHUNK) ? total - base : CHUNK;
#ifdef FS_BUF
            // 2-pass like read_array_slice: gmem -> cb (burst) THEN cb -> FIFO (the framework path)
            float cb[CHUNK];
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                cb[i] = u2f(gmem[xoff + base + i]);
            }
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                ld_data.write(cb[i]);
            }
#else
            for (int i = 0; i < k; ++i) {     // direct gmem -> FIFO (one pass)
#pragma HLS PIPELINE II=1
                ld_data.write(u2f(gmem[xoff + base + i]));
            }
#endif
        }
    }
}

void fs_compute(hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data,
                hls::stream<Cmd>& cp_ctrl, hls::stream<float>& cp_data) {
    float h[T];
#pragma HLS ARRAY_PARTITION variable=h complete
    for (int t = 0; t < T; ++t) h[t] = (float)((t + 1) * 0.25f - 1.0f);   // constant taps (no h-read)
    bool done = false;
    COMP: while (!done) {
        Cmd c = ld_ctrl.read();
        cp_ctrl.write(c);
        if (c.done) { done = true; break; }
        ROWS: for (int r = 0; r < c.nr; ++r) {
            float sr[T];
#pragma HLS ARRAY_PARTITION variable=sr complete
            for (int k = 0; k < T; ++k) sr[k] = 0.0f;      // per-row window flush
            COLS: for (int s = 0; s < c.nc; ++s) {
#pragma HLS PIPELINE II=1
                float x = ld_data.read();
                for (int k = T - 1; k >= 1; --k) sr[k] = sr[k - 1];
                sr[0] = x;
                if (s >= T - 1) {
                    float acc = 0.0f;
                    for (int t = 0; t < T; ++t) acc += h[t] * sr[t];
                    cp_data.write(acc);                    // read n_cols, write n_cols-T+1
                }
            }
        }
    }
}

void fs_store(hls::stream<Cmd>& cp_ctrl, hls::stream<float>& cp_data,
              ap_uint<MEM_DW>* gmem, hls::stream<ap_uint<32> >& m_out) {
    bool done = false;
    STORE: while (!done) {
        Cmd c = cp_ctrl.read();
        if (c.done) { done = true; break; }
        int total = c.nr * (c.nc - T + 1);
        SY: for (int base = 0; base < total; base += CHUNK) {
            int k = (total - base < CHUNK) ? total - base : CHUNK;
#ifdef FS_BUF
            // 2-pass like write_array_slice: FIFO -> cb THEN cb -> gmem (burst) (the framework path)
            float cb[CHUNK];
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                cb[i] = cp_data.read();
            }
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                gmem[c.yoff + base + i] = f2u(cb[i]);
            }
#else
            for (int i = 0; i < k; ++i) {     // direct FIFO -> gmem (one pass)
#pragma HLS PIPELINE II=1
                gmem[c.yoff + base + i] = f2u(cp_data.read());
            }
#endif
        }
        m_out.write((ap_uint<32>)total);
    }
}
}  // namespace

void fir_skel(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
              ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem offset=slave depth=16384
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    hls::stream<Cmd> ld_ctrl("ld_ctrl"), cp_ctrl("cp_ctrl");
#pragma HLS STREAM variable=ld_ctrl depth=16
#pragma HLS STREAM variable=cp_ctrl depth=16
    hls::stream<float> ld_data("ld_data"), cp_data("cp_data");
#pragma HLS STREAM variable=ld_data depth=LSDEPTH
#pragma HLS STREAM variable=cp_data depth=LSDEPTH
    fs_load(s_in, gmem, ld_ctrl, ld_data);
    fs_compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    fs_store(cp_ctrl, cp_data, gmem, m_out);
}
