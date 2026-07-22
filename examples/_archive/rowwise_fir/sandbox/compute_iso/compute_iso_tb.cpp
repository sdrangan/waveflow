// compute_iso_tb.cpp — one (nrow, ncol) per cosim run; the transaction latency = compute time.
#include <ap_int.h>
#include <cstdio>
#include <cstdlib>
#include <hls_stream.h>

#define MEM_DW 32
static const int MAXE = 1024;
void compute_iso(const float xbuf[MAXE], hls::stream<ap_uint<MEM_DW> >& cmd_in,
                 hls::stream<ap_uint<MEM_DW> >& resp_out);

int main(int argc, char** argv) {
    int nrow = (argc > 1) ? std::atoi(argv[1]) : 4;
    int ncol = (argc > 2) ? std::atoi(argv[2]) : 64;
    static float xbuf[MAXE];
    for (int i = 0; i < nrow * ncol && i < MAXE; ++i) xbuf[i] = (float)((i % 17) * 0.5f - 4.0f);
    hls::stream<ap_uint<MEM_DW> > cmd_in("cmd_in"), resp_out("resp_out");
    cmd_in.write((ap_uint<MEM_DW>)nrow);
    cmd_in.write((ap_uint<MEM_DW>)ncol);
    compute_iso(xbuf, cmd_in, resp_out);
    if (resp_out.empty()) { std::fprintf(stderr, "WAVEFLOW_ERROR: no resp\n"); return 1; }
    (void)resp_out.read();
    std::printf("WAVEFLOW_COMPUTE_ISO_OK: nrow %d ncol %d\n", nrow, ncol);
    return 0;
}
