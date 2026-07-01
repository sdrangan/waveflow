// fir_skel_tb.cpp — NJOBS back-to-back FIR jobs of (NROW x NCOL) (one cosim run).
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define MEM_DW 32
static const int T = 8;
static const unsigned END_N = 0xFFFFFFFFu;
void fir_skel(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
              ap_uint<MEM_DW>* gmem);

int main(int argc, char** argv) {
    int nr = (argc > 1) ? std::atoi(argv[1]) : 4;
    int nc = (argc > 2) ? std::atoi(argv[2]) : 64;
    int nj = (argc > 3) ? std::atoi(argv[3]) : 6;
    int outlen = nc - T + 1;
    std::vector<ap_uint<MEM_DW> > gmem(16384, 0);
    hls::stream<ap_uint<32> > s_in("s_in"), m_out("m_out");
    unsigned off = 0;
    for (int j = 0; j < nj; ++j) {
        unsigned xoff = off; off += (unsigned)(nr * nc);
        unsigned yoff = off; off += (unsigned)(nr * outlen);
        for (int i = 0; i < nr * nc; ++i) {
            union { float f; unsigned u; } cv; cv.f = (float)((i % 17) * 0.5f - 4.0f + j);
            gmem[xoff + i] = (ap_uint<MEM_DW>)cv.u;
        }
        s_in.write((ap_uint<32>)nr);
        s_in.write((ap_uint<32>)nc);
        s_in.write((ap_uint<32>)xoff);
        s_in.write((ap_uint<32>)yoff);
    }
    s_in.write((ap_uint<32>)END_N);
    fir_skel(s_in, m_out, gmem.data());
    int resp = 0;
    while (!m_out.empty()) { (void)m_out.read(); ++resp; }
    if (resp != nj) { std::fprintf(stderr, "WAVEFLOW_ERROR: fir_skel %dx%d nj=%d got %d\n", nr, nc, nj, resp); return 1; }
    std::printf("WAVEFLOW_SUCCESS: fir_skel %dx%d nj=%d (%d resp)\n", nr, nc, nj, resp);
    return 0;
}
