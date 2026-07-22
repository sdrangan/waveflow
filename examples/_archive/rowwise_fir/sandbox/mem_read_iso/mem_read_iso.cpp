// mem_read_iso.cpp — isolate the m_axi read FORM that broke cosim in the FIR hook.
//
// Same free-running DATAFLOW load->store copy as loadstore_iso (ap_ctrl_hs + while(!done) + END +
// one gmem bundle), but the LOAD's read of each element is selected by -DREAD_MODE:
//
//   0  indexed direct      : x = u2f(mem[xoff + i])                 (the fix; loadstore_iso form)
//   1  helper, recomputed  : my_read_lane(mem + xoff + i, &x, 1)    (read_array_slice's wp+e*WPU form)
//   2  helper, running ptr : my_read_lane(p, &x, 1); ++p            (the form that FAILED in the hook)
//   3  direct, base+index  : x = u2f(p[i])                          (pointer, but indexed, no ++)
//   4  direct, running ptr : x = u2f(*p); ++p                       (running ptr WITHOUT the helper)
//
// `my_read_lane` replicates read_array_lane's shape exactly: #pragma HLS INLINE + a nullptr/n guard
// + a single src[0] read through a pointer parameter — so no generated headers are needed and the
// helper structure is the controlled variable.  The tb checks the copy is bit-exact; a mode that
// reads the first element as 0 (the observed RTL failure) produces a mismatch at element 0.
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdint>

#define MEM_DW 32
static const unsigned END_N = 0xFFFFFFFFu;
#ifndef READ_MODE
#define READ_MODE 0
#endif

namespace {
struct Cmd { int n; unsigned xoff; unsigned yoff; bool done; };

inline float u2f(ap_uint<MEM_DW> u) { union { unsigned i; float f; } c; c.i = (unsigned)u; return c.f; }
inline ap_uint<MEM_DW> f2u(float f) { union { unsigned i; float f; } c; c.f = f; return ap_uint<MEM_DW>(c.i); }

// Replica of read_array_lane<32>: INLINE + guard + one read through the pointer param.
inline void my_read_lane(const ap_uint<MEM_DW>* src, float* out, int n) {
#pragma HLS INLINE
    if (n > 0 && src != nullptr) out[0] = u2f(src[0]);
}

void mr_load(hls::stream<ap_uint<32> >& s_in, const ap_uint<MEM_DW>* mem,
             hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data) {
    bool done = false;
    LOAD: while (!done) {
        unsigned n = (unsigned)s_in.read();
        if (n == END_N) { Cmd c; c.n = 0; c.xoff = 0; c.yoff = 0; c.done = true; ld_ctrl.write(c); break; }
        unsigned xoff = (unsigned)s_in.read(), yoff = (unsigned)s_in.read();
        Cmd c; c.n = (int)n; c.xoff = xoff; c.yoff = yoff; c.done = false;
        ld_ctrl.write(c);
        const ap_uint<MEM_DW>* p = mem + xoff;             // running/base pointer (modes 2,3,4)
        LX: for (int i = 0; i < (int)n; ++i) {
#pragma HLS PIPELINE II=1
            float x;
#if READ_MODE == 0
            x = u2f(mem[xoff + i]);
#elif READ_MODE == 1
            my_read_lane(mem + xoff + i, &x, 1);
#elif READ_MODE == 2
            my_read_lane(p, &x, 1); ++p;
#elif READ_MODE == 3
            x = u2f(p[i]);
#elif READ_MODE == 4
            x = u2f(*p); ++p;
#endif
            ld_data.write(x);
        }
    }
}

void mr_store(hls::stream<Cmd>& ld_ctrl, hls::stream<float>& ld_data,
              ap_uint<MEM_DW>* mem, hls::stream<ap_uint<32> >& m_out) {
    bool done = false;
    STORE: while (!done) {
        Cmd c = ld_ctrl.read();
        if (c.done) { done = true; break; }
        SY: for (int i = 0; i < c.n; ++i) {
#pragma HLS PIPELINE II=1
            mem[c.yoff + i] = f2u(ld_data.read());
        }
        m_out.write((ap_uint<32>)c.n);
    }
}
}  // namespace

void mem_read_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
                  ap_uint<MEM_DW>* mem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=mem bundle=gmem offset=slave depth=8192
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    hls::stream<Cmd> ld_ctrl("ld_ctrl");
#pragma HLS STREAM variable=ld_ctrl depth=8
    hls::stream<float> ld_data("ld_data");
#pragma HLS STREAM variable=ld_data depth=1024
    mr_load(s_in, mem, ld_ctrl, ld_data);
    mr_store(ld_ctrl, ld_data, mem, m_out);
}
