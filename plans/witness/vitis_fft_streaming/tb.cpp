#include <cstdio>
#include <cstdlib>
#include "types.hpp"
#ifdef OUT_PATCH
#define OUTF "C:/Users/sdran/AppData/Local/Temp/amdfft/out_patch.txt"
#else
#define OUTF "C:/Users/sdran/AppData/Local/Temp/amdfft/out_ref.txt"
#endif
#ifndef NF
#define NF 16
#endif
int main() {
    hls::stream<T_in> in[FR];
    hls::stream<T_out> out[FR];
    srand(1);
    // Every frame's input queued up front, then the top called back to back: cosim drives the
    // RTL with the next ap_start as soon as the design is ready for it.
    for (int f = 0; f < NF; f++)
        for (int i = 0; i < FL / FR; i++)
            for (int r = 0; r < FR; r++)
                in[r].write(T_in(ap_fixed<16, 2>((rand() % 2000 - 1000) / 1024.0),
                                 ap_fixed<16, 2>((rand() % 2000 - 1000) / 1024.0)));
    for (int f = 0; f < NF; f++) fft_top(in, out);
    long n = 0;
    FILE* fo = fopen(OUTF, "w");
    for (int r = 0; r < FR; r++) while (!out[r].empty()) {
        T_out v = out[r].read(); n++;
        fprintf(fo, "%d %s %s\n", r, v.real().to_string(16).c_str(), v.imag().to_string(16).c_str());
    }
    fclose(fo);
    printf("read %ld outputs (expect %d)\n", n, NF * FL);
    return n == NF * FL ? 0 : 1;
}
