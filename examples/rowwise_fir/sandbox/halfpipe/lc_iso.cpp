// lc_iso.cpp — half-pipe: real m_axi LOAD + compute + FAKE store (no m_axi write).
//
// Isolates the read-side: load reads X from gmem (m_axi READ), compute is the trivial II=1
// pass-through, and "store" folds the result into a checksum (no m_axi write). Only the READ
// channel touches the bus. Multi-job, free-running. If period ~= n (1 cyc/sample) the read-side +
// compute is clean; if it's ~2n the read+compute interaction itself is the slowdown.
#include <ap_int.h>
#include <hls_stream.h>

#define MEM_DW 32
static const int CHUNK = 256;
static const unsigned END_N = 0xFFFFFFFFu;
#ifndef LSDEPTH
#define LSDEPTH 1024
#endif

namespace {
struct Cmd { int n; unsigned xoff; bool done; };

void lc_load(hls::stream<ap_uint<32> >& s_in, const ap_uint<MEM_DW>* gmem,
             hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned n = (unsigned)s_in.read();
        if (n == END_N) { Cmd c; c.n = 0; c.xoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned xoff = (unsigned)s_in.read();
        Cmd c; c.n = (int)n; c.xoff = xoff; c.done = false;
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

void lc_compute(hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data,
                hls::stream<Cmd>& cp_ctrl, hls::stream<ap_uint<MEM_DW> >& cp_data) {
    bool done = false;
    COMP: while (!done) {
        Cmd c = ld_ctrl.read();
        cp_ctrl.write(c);
        if (c.done) { done = true; break; }
        CC: for (int i = 0; i < c.n; ++i) {
#pragma HLS PIPELINE II=1
            cp_data.write(ld_data.read() + 1);
        }
    }
}

void lc_fakestore(hls::stream<Cmd>& cp_ctrl, hls::stream<ap_uint<MEM_DW> >& cp_data,
                  hls::stream<ap_uint<32> >& m_out) {
    bool done = false;
    STORE: while (!done) {
        Cmd c = cp_ctrl.read();
        if (c.done) { done = true; break; }
        ap_uint<32> chk = 0;
        SY: for (int i = 0; i < c.n; ++i) {
#pragma HLS PIPELINE II=1
            chk ^= cp_data.read();            // fold (no m_axi write) — keeps the datapath live
        }
        m_out.write(chk);
    }
}
}  // namespace

void lc_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
            const ap_uint<MEM_DW>* gmem) {
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
    lc_load(s_in, gmem, ld_ctrl, ld_data);
    lc_compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    lc_fakestore(cp_ctrl, cp_data, m_out);
}
