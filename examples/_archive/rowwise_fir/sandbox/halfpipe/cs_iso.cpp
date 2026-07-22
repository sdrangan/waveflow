// cs_iso.cpp — half-pipe: FAKE load (from BRAM) + compute + real m_axi STORE (no m_axi read).
//
// Isolates the write-side: load reads X from an on-chip BRAM port (no m_axi read), compute is the
// trivial II=1 pass-through, store writes Y to gmem (m_axi WRITE). Only the WRITE channel touches
// the bus. Multi-job, free-running. Mirror of lc_iso for the opposite direction.
#include <ap_int.h>
#include <hls_stream.h>

#define MEM_DW 32
static const int CHUNK = 256;
static const int MAXE = 2048;
static const unsigned END_N = 0xFFFFFFFFu;
#ifndef LSDEPTH
#define LSDEPTH 1024
#endif

namespace {
struct Cmd { int n; unsigned yoff; bool done; };

void cs_fakeload(hls::stream<ap_uint<32> >& s_in, const float xbuf[MAXE],
                 hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned n = (unsigned)s_in.read();
        if (n == END_N) { Cmd c; c.n = 0; c.yoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned yoff = (unsigned)s_in.read();
        Cmd c; c.n = (int)n; c.yoff = yoff; c.done = false;
        ld_ctrl.write(c);
        LX: for (int i = 0; i < (int)n; ++i) {
#pragma HLS PIPELINE II=1
            union { float f; unsigned u; } cv; cv.f = xbuf[i];   // BRAM read (no m_axi)
            ld_data.write(ap_uint<MEM_DW>(cv.u));
        }
    }
}

void cs_compute(hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data,
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

void cs_store(hls::stream<Cmd>& cp_ctrl, hls::stream<ap_uint<MEM_DW> >& cp_data,
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

void cs_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
            const float xbuf[MAXE], ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE ap_memory port=xbuf
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem offset=slave depth=8192
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    hls::stream<Cmd> ld_ctrl("ld_ctrl"), cp_ctrl("cp_ctrl");
#pragma HLS STREAM variable=ld_ctrl depth=16
#pragma HLS STREAM variable=cp_ctrl depth=16
    hls::stream<ap_uint<MEM_DW> > ld_data("ld_data"), cp_data("cp_data");
#pragma HLS STREAM variable=ld_data depth=LSDEPTH
#pragma HLS STREAM variable=cp_data depth=LSDEPTH
    cs_fakeload(s_in, xbuf, ld_ctrl, ld_data);
    cs_compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    cs_store(cp_ctrl, cp_data, gmem, m_out);
}
