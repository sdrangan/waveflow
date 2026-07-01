// lc_iso_tb.cpp — NJOBS load+compute jobs of N words (real m_axi read, fake store).
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define MEM_DW 32
static const unsigned END_N = 0xFFFFFFFFu;
void lc_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
            const ap_uint<MEM_DW>* gmem);

int main(int argc, char** argv) {
    int n  = (argc > 1) ? std::atoi(argv[1]) : 256;
    int nj = (argc > 2) ? std::atoi(argv[2]) : 6;
    std::vector<ap_uint<MEM_DW> > gmem(8192, 0);
    hls::stream<ap_uint<32> > s_in("s_in"), m_out("m_out");
    for (int j = 0; j < nj; ++j) {
        unsigned xoff = (unsigned)(j * n);
        for (int i = 0; i < n; ++i) gmem[xoff + i] = (ap_uint<MEM_DW>)(i + 3 * j + 1);
        s_in.write((ap_uint<32>)n);
        s_in.write((ap_uint<32>)xoff);
    }
    s_in.write((ap_uint<32>)END_N);
    lc_iso(s_in, m_out, gmem.data());
    int resp = 0;
    while (!m_out.empty()) { (void)m_out.read(); ++resp; }
    if (resp != nj) { std::fprintf(stderr, "WAVEFLOW_ERROR: lc N=%d nj=%d got %d resp\n", n, nj, resp); return 1; }
    std::printf("WAVEFLOW_SUCCESS: lc N=%d nj=%d (%d resp)\n", n, nj, resp);
    return 0;
}
