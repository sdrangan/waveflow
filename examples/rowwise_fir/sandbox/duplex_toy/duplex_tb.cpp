// duplex_tb.cpp — drive one (mode, N) per cosim run; the cycle count is the transaction latency.
#include <ap_int.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define MEM_DW 32
extern "C" void duplex(ap_uint<MEM_DW>* gmem, int n, int mode, ap_uint<MEM_DW>* ret);

int main(int argc, char** argv) {
    int mode = (argc > 1) ? std::atoi(argv[1]) : 2;
    int n = (argc > 2) ? std::atoi(argv[2]) : 1024;
    std::vector<ap_uint<MEM_DW>> gmem(8192, 0);
    std::vector<ap_uint<MEM_DW>> ret(4, 0);
    for (int i = 0; i < n; i++) gmem[i] = (ap_uint<MEM_DW>)(i * 3 + 1);

    duplex(gmem.data(), n, mode, ret.data());

    int fails = 0;
    if (mode == 2 || mode == 3) {
        for (int i = 0; i < n; i++) {
            uint32_t got = (uint32_t)gmem[n + i], exp = (uint32_t)(i * 3 + 1) + 1;
            if (got != exp) { if (fails < 5) std::fprintf(stderr, "rw[%d] %u != %u\n", i, got, exp); ++fails; }
        }
    } else if (mode == 1) {
        for (int i = 0; i < n; i++) if ((uint32_t)gmem[n + i] != (uint32_t)i) { ++fails; break; }
    }
    if (fails) { std::fprintf(stderr, "WAVEFLOW_ERROR: duplex mode %d N %d FAILED (%d)\n", mode, n, fails); return 1; }
    std::printf("WAVEFLOW_DUPLEX_OK: mode %d N %d\n", mode, n);
    return 0;
}
