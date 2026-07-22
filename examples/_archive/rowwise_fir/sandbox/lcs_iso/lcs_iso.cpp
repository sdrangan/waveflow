// lcs_iso.cpp — Rung 3: free-running load -> compute -> store, multi-job, with a DEPTH knob.
//
// Rung 2 showed load||store (no compute) overlaps read+write (period ~= max). This inserts a
// MIDDLE stage between them — a trivial II=1 pass-through (y = x + 1), so it carries compute's
// *position and timing* without the FIR specifics — and exposes the inter-stage FIFO DEPTH as a
// compile knob (-DLSDEPTH). Question:
//   * Does adding a middle stage collapse the period from ~max back to ~sum? (then it's structural)
//   * Does increasing DEPTH (letting load run more jobs ahead) restore period -> ~max?
// This isolates whether the FIR's lost full-duplex is a depth/run-ahead problem or a deeper one.
#include <ap_int.h>
#include <hls_stream.h>

#define MEM_DW 32
static const int CHUNK = 256;
static const unsigned END_N = 0xFFFFFFFFu;
#ifndef LSDEPTH
#define LSDEPTH 1024            // inter-stage data-FIFO depth (the run-ahead knob)
#endif

namespace {
struct Cmd { int n; unsigned xoff; unsigned yoff; bool done; };

void lcs_load(hls::stream<ap_uint<32> >& s_in, const ap_uint<MEM_DW>* gmem,
              hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned n = (unsigned)s_in.read();
        if (n == END_N) { Cmd c; c.n = 0; c.xoff = 0; c.yoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned xoff = (unsigned)s_in.read(), yoff = (unsigned)s_in.read();
        Cmd c; c.n = (int)n; c.xoff = xoff; c.yoff = yoff; c.done = false;
        ld_ctrl.write(c);
        LX: for (int base = 0; base < (int)n; base += CHUNK) {
            int k = ((int)n - base < CHUNK) ? (int)n - base : CHUNK;
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                ld_data.write(gmem[xoff + base + i]);
            }
        }
    }
}

void lcs_compute(hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data,
                 hls::stream<Cmd>& cp_ctrl, hls::stream<ap_uint<MEM_DW> >& cp_data) {
    bool done = false;
    COMP: while (!done) {
        Cmd c = ld_ctrl.read();
        cp_ctrl.write(c);
        if (c.done) { done = true; break; }
        CC: for (int i = 0; i < c.n; ++i) {
#pragma HLS PIPELINE II=1
            cp_data.write(ld_data.read() + 1);     // trivial II=1 pass-through (compute's stand-in)
        }
    }
}

void lcs_store(hls::stream<Cmd>& cp_ctrl, hls::stream<ap_uint<MEM_DW> >& cp_data,
               ap_uint<MEM_DW>* gmem, hls::stream<ap_uint<32> >& m_out) {
    bool done = false;
    STORE: while (!done) {
        Cmd c = cp_ctrl.read();
        if (c.done) { done = true; break; }
        SY: for (int base = 0; base < c.n; base += CHUNK) {
            int k = (c.n - base < CHUNK) ? c.n - base : CHUNK;
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                gmem[c.yoff + base + i] = cp_data.read();
            }
        }
        m_out.write((ap_uint<32>)c.n);
    }
}
}  // namespace

void lcs_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
             ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem offset=slave depth=8192
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    hls::stream<Cmd> ld_ctrl("ld_ctrl"), cp_ctrl("cp_ctrl");
#pragma HLS STREAM variable=ld_ctrl depth=16
#pragma HLS STREAM variable=cp_ctrl depth=16
    hls::stream<ap_uint<MEM_DW> > ld_data("ld_data"), cp_data("cp_data");
#pragma HLS STREAM variable=ld_data depth=LSDEPTH
#pragma HLS STREAM variable=cp_data depth=LSDEPTH
    lcs_load(s_in, gmem, ld_ctrl, ld_data);
    lcs_compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    lcs_store(cp_ctrl, cp_data, gmem, m_out);
}
