// lcs_iso_tb.cpp — NJOBS back-to-back load->compute(+1)->store jobs of N words (one cosim run).
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define MEM_DW 32
static const unsigned END_N = 0xFFFFFFFFu;
void lcs_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
             ap_uint<MEM_DW>* gmem);

int main(int argc, char** argv) {
    int n  = (argc > 1) ? std::atoi(argv[1]) : 256;
    int nj = (argc > 2) ? std::atoi(argv[2]) : 4;
    std::vector<ap_uint<MEM_DW> > gmem(8192, 0);
    hls::stream<ap_uint<32> > s_in("s_in"), m_out("m_out");

    for (int j = 0; j < nj; ++j) {
        unsigned xoff = 2u * j * n, yoff = 2u * j * n + n;
        for (int i = 0; i < n; ++i) gmem[xoff + i] = (ap_uint<MEM_DW>)(i + 7 * j + 1);
        s_in.write((ap_uint<32>)n);
        s_in.write((ap_uint<32>)xoff);
        s_in.write((ap_uint<32>)yoff);
    }
    s_in.write((ap_uint<32>)END_N);

    lcs_iso(s_in, m_out, gmem.data());

    int resp = 0;
    while (!m_out.empty()) { (void)m_out.read(); ++resp; }
    int fails = (resp != nj) ? 1 : 0;
    for (int j = 0; j < nj && !fails; ++j) {
        unsigned xoff = 2u * j * n, yoff = 2u * j * n + n;
        for (int i = 0; i < n; ++i)
            if ((unsigned)gmem[yoff + i] != (unsigned)gmem[xoff + i] + 1) { fails = 1; break; }
    }
    if (fails) { std::fprintf(stderr, "WAVEFLOW_ERROR: lcs N=%d nj=%d FAILED\n", n, nj); return 1; }
    std::printf("WAVEFLOW_SUCCESS: lcs N=%d nj=%d (%d resp)\n", n, nj, resp);
    return 0;
}
