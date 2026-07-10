#include <ap_int.h>
#include <hls_stream.h>
#include "memmgr_tb.hpp"
#include "float32_array_utils.h"
#include "int32_array_utils.h"
#include "cmd.h"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#ifndef MEM_DW
#define MEM_DW 64
#endif
void interleaver(hls::stream<Cmd>& s_cmd, hls::stream<ap_uint<32> >& s_done,
                 const ap_uint<MEM_DW>* in_mem, ap_uint<MEM_DW>* out_mem);

static uint32_t fbits(float f) { union { float f; uint32_t u; } c; c.f = f; return c.u; }

int main(int argc, char** argv) {
    int n  = (argc > 1) ? std::atoi(argv[1]) : 256;
    int nj = (argc > 2) ? std::atoi(argv[2]) : 4;
    const int MEM_SIZE = 8192;
    static ap_uint<MEM_DW> mem[MEM_SIZE] = {};
    waveflow::memmgr::MemMgr<MEM_DW> mgr(mem, MEM_SIZE);
    hls::stream<Cmd> s_cmd("s_cmd");
    hls::stream<ap_uint<32> > s_done("s_done");
    std::vector<std::vector<ap_int<32> > > Pall(nj);
    std::vector<std::vector<float> > Xall(nj);
    std::vector<int> yw(nj);
    const unsigned bpw = MEM_DW / 8;

    for (int j = 0; j < nj; ++j) {
        std::vector<ap_int<32> > Pd(n);
        std::vector<float> Xd(n);
        for (int i = 0; i < n; ++i) { Pd[i] = (i * 13 + 5) % n; Xd[i] = (float)(i * 0.5f - 3.0f + j); }
        int pw   = mgr.alloc(int32_array_utils::get_nwords<MEM_DW>(n));
        int xw   = mgr.alloc(float32_array_utils::get_nwords<MEM_DW>(n));
        int yw_j = mgr.alloc(float32_array_utils::get_nwords<MEM_DW>(n));
        int32_array_utils::write_array_slice<MEM_DW>(Pd.data(), mem + pw, 0, n);
        float32_array_utils::write_array_slice<MEM_DW>(Xd.data(), mem + xw, 0, n);
        Cmd c; c.n = n; c.p_addr = pw * bpw; c.x_addr = xw * bpw; c.y_addr = yw_j * bpw;
        s_cmd.write(c);
        Pall[j] = Pd; Xall[j] = Xd; yw[j] = yw_j;
    }

    interleaver(s_cmd, s_done, mem, mem);          // one physical memory behind both ports
    for (int j = 0; j < nj; ++j) (void)s_done.read();   // blocking sync: N completions

    int fails = 0;
    for (int j = 0; j < nj; ++j) {
        std::vector<float> Yout(n);
        float32_array_utils::read_array_slice<MEM_DW>(mem + yw[j], 0, n, Yout.data());
        for (int i = 0; i < n; ++i) {
            float exp = Xall[j][(int)Pall[j][i]];
            if (fbits(Yout[i]) != fbits(exp)) {
                if (fails < 4) std::fprintf(stderr, "  job %d elem %d: got 0x%08x exp 0x%08x\n",
                                            j, i, fbits(Yout[i]), fbits(exp));
                ++fails;
            }
        }
    }
    if (fails) { std::fprintf(stderr, "WAVEFLOW_ERROR: interleaver_task mismatch, %d elems\n", fails); return 1; }
    std::printf("WAVEFLOW_SUCCESS: interleaver_task n=%d nj=%d free-running hls::task bit-exact\n", n, nj);
    return 0;
}
