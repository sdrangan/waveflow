// tb5.cpp -- two jobs of w5_prog.S (assembled by the Vitis 2025.1 GNU as, rv32im_zicsr_zve32x) through
// the scalar + vector tasks; each must end with a4 = 48 + 8 = 56 and not hang.
#include <cstdio>
#include <ap_int.h>
#include <hls_stream.h>

typedef ap_uint<32> u32;
void w5_top(hls::stream<u32> &prog, hls::stream<u32> &res);

static const unsigned P[] = {0x00800513, 0x0d0572d7, 0x00300593, 0x5e05c0d7, 0x02108157,
                             0x5e0041d7, 0x0221a257, 0x42402657, 0x00000713, 0x00170713,
                             0xfec71ee3, 0x00570733, 0x00000073};

int main() {
    hls::stream<u32> prog, res;
    const int n = sizeof(P) / sizeof(P[0]);
    int bad = 0;
    for (int job = 0; job < 2; job++) {
        prog.write(n);
        for (int i = 0; i < n; i++) prog.write(P[i]);
    }
    w5_top(prog, res);
    for (int job = 0; job < 2; job++) {
        unsigned r = res.read();
        printf("w5: job %d a4 = %u (want 56)\n", job, r);
        bad += (r != 56);
    }
    printf("w5: %s\n", bad ? "FAIL" : "PASS");
    return bad;
}
