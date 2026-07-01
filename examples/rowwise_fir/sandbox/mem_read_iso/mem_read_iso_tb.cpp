// mem_read_iso_tb.cpp — NJOBS copy jobs of N words; check bit-exact + report first mismatch index.
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define MEM_DW 32
static const unsigned END_N = 0xFFFFFFFFu;
void mem_read_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
                  ap_uint<MEM_DW>* mem);

static ap_uint<MEM_DW> f2u(float f) { union { unsigned i; float f; } c; c.f = f; return ap_uint<MEM_DW>(c.i); }

int main(int argc, char** argv) {
    int n  = (argc > 1) ? std::atoi(argv[1]) : 64;
    int nj = (argc > 2) ? std::atoi(argv[2]) : 3;
    std::vector<ap_uint<MEM_DW> > mem(8192, 0);
    hls::stream<ap_uint<32> > s_in("s_in"), m_out("m_out");
    for (int j = 0; j < nj; ++j) {
        unsigned xoff = 2u * j * n, yoff = 2u * j * n + n;
        for (int i = 0; i < n; ++i) mem[xoff + i] = f2u((float)((i % 17) * 0.5f - 4.0f + j));  // x[0] != 0
        s_in.write((ap_uint<32>)n);
        s_in.write((ap_uint<32>)xoff);
        s_in.write((ap_uint<32>)yoff);
    }
    s_in.write((ap_uint<32>)END_N);
    mem_read_iso(s_in, m_out, mem.data());
    while (!m_out.empty()) (void)m_out.read();

    int fails = 0;
    for (int j = 0; j < nj; ++j) {
        unsigned xoff = 2u * j * n, yoff = 2u * j * n + n;
        for (int i = 0; i < n; ++i) {
            if ((uint32_t)mem[yoff + i] != (uint32_t)mem[xoff + i]) {
                if (fails < 4)
                    std::fprintf(stderr, "  job %d elem %d: got 0x%08x exp 0x%08x\n",
                                 j, i, (uint32_t)mem[yoff + i], (uint32_t)mem[xoff + i]);
                ++fails;
            }
        }
    }
    if (fails) { std::fprintf(stderr, "WAVEFLOW_ERROR: READ_MODE mismatch, %d elems\n", fails); return 1; }
    std::printf("WAVEFLOW_SUCCESS: mem_read_iso N=%d nj=%d bit-exact\n", n, nj);
    return 0;
}
