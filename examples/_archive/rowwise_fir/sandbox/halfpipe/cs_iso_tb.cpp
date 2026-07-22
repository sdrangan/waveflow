// cs_iso_tb.cpp — NJOBS compute+store jobs of N words (fake BRAM load, real m_axi write).
#include <ap_int.h>
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define MEM_DW 32
static const int MAXE = 2048;
static const unsigned END_N = 0xFFFFFFFFu;
void cs_iso(hls::stream<ap_uint<32> >& s_in, hls::stream<ap_uint<32> >& m_out,
            const float xbuf[MAXE], ap_uint<MEM_DW>* gmem);

int main(int argc, char** argv) {
    int n  = (argc > 1) ? std::atoi(argv[1]) : 256;
    int nj = (argc > 2) ? std::atoi(argv[2]) : 6;
    static float xbuf[MAXE];
    for (int i = 0; i < n && i < MAXE; ++i) xbuf[i] = (float)((i % 17) * 0.5f - 4.0f);
    std::vector<ap_uint<MEM_DW> > gmem(8192, 0);
    hls::stream<ap_uint<32> > s_in("s_in"), m_out("m_out");
    for (int j = 0; j < nj; ++j) {
        unsigned yoff = (unsigned)(j * n);
        s_in.write((ap_uint<32>)n);
        s_in.write((ap_uint<32>)yoff);
    }
    s_in.write((ap_uint<32>)END_N);
    cs_iso(s_in, m_out, xbuf, gmem.data());
    int resp = 0;
    while (!m_out.empty()) { (void)m_out.read(); ++resp; }
    if (resp != nj) { std::fprintf(stderr, "WAVEFLOW_ERROR: cs N=%d nj=%d got %d resp\n", n, nj, resp); return 1; }
    std::printf("WAVEFLOW_SUCCESS: cs N=%d nj=%d (%d resp)\n", n, nj, resp);
    return 0;
}
