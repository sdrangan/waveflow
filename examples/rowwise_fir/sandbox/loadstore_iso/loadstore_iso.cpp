// loadstore_iso.cpp — Rung 2 of the FIR-overlap ladder: free-running load ‖ store, NO compute.
//
// Same free-running DATAFLOW shape as the FIR (ap_ctrl_hs + while(!done) stages + END drain + one
// shared `gmem` m_axi bundle + a deep ld_data FIFO so load can run a job ahead), but the compute
// stage is removed — `store` just copies what `load` read. Multi-job, back-to-back.
//
// Question: does load(job N)'s read burst overlap store(job N-1)'s write burst on the one bundle?
//   period -> max(read,write)/job   => YES, the per-job free-running structure exploits full-duplex
//                                       (so COMPUTE is what de-phases them in the real FIR)
//   period -> read+write /job       => NO, the per-job/burst structure itself serializes the bus
// The duplex toy already proved the *bundle* is full-duplex; this asks whether *this structure* uses it.
#include <ap_int.h>
#include <hls_stream.h>

#define MEM_DW 32
static const int CHUNK = 256;
static const unsigned END_N = 0xFFFFFFFFu;

namespace {
struct Cmd { int n; unsigned xoff; unsigned yoff; bool done; };

void ls_load(hls::stream<ap_uint<32> >& s_in, const ap_uint<MEM_DW>* gmem,
             hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned n = (unsigned)s_in.read();
        if (n == END_N) { Cmd c; c.n = 0; c.xoff = 0; c.yoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned xoff = (unsigned)s_in.read();
        unsigned yoff = (unsigned)s_in.read();
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

void ls_store(hls::stream<Cmd>& ld_ctrl, hls::stream<ap_uint<MEM_DW> >& ld_data,
              ap_uint<MEM_DW>* gmem, hls::stream<ap_uint<32> >& m_out) {
    bool done = false;
    STORE: while (!done) {
        Cmd c = ld_ctrl.read();
        if (c.done) { done = true; break; }
        SY: for (int base = 0; base < c.n; base += CHUNK) {
            int k = (c.n - base < CHUNK) ? c.n - base : CHUNK;
            for (int i = 0; i < k; ++i) {
#pragma HLS PIPELINE II=1
                gmem[c.yoff + base + i] = ld_data.read();
            }
        }
        m_out.write((ap_uint<32>)c.n);
    }
}
}  // namespace

void loadstore_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
                   ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
// max_burst_length=256 matches the generated kernels (hwgen kernel_signature), so this pure-copy
// kernel characterizes the bus the SAME way the FIR drives it (used by bus_characterize.py).
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem offset=slave depth=8192 max_read_burst_length=256 max_write_burst_length=256
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    hls::stream<Cmd> ld_ctrl("ld_ctrl");
#pragma HLS STREAM variable=ld_ctrl depth=8
    hls::stream<ap_uint<MEM_DW> > ld_data("ld_data");
#pragma HLS STREAM variable=ld_data depth=1024
    ls_load(s_in, gmem, ld_ctrl, ld_data);
    ls_store(ld_ctrl, ld_data, gmem, m_out);
}
