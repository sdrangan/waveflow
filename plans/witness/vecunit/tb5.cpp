// tb5.cpp -- witness 5's check: two jobs of two blocks each through w5_top. Each job copies all 8
// column groups of two input blocks (POP after each) into two output blocks (PUSH after each); the
// output must be every input lane group plus one, in order.
#include <cstdio>
#define EXPLICIT_ACQUIRE_RELEASE
#include <ap_int.h>
#include <hls_stream.h>

typedef ap_uint<96> lg_t;
typedef ap_uint<64> word_t;
void w5_top(hls::stream<lg_t>& in, hls::stream<word_t>& prog, hls::stream<lg_t>& out,
            hls::stream<word_t>& status);

static word_t ins(int op, int g) {
    word_t w = 0;
    w(6, 0) = op;
    w(51, 40) = g;
    return w;
}

int main() {
    hls::stream<lg_t> in, out;
    hls::stream<word_t> prog, status;
    const int jobs = 2, blocks = 2;
    for (int b = 0; b < jobs * blocks; ++b)
        for (int i = 0; i < 64; ++i) in.write(lg_t(1000 * b + i));
    for (int j = 0; j < jobs; ++j) {
        word_t job = 0;
        job(15, 0) = 50 + j;
        job(31, 16) = blocks * 10;
        prog.write(job);
        for (int b = 0; b < blocks; ++b) {
            for (int g = 0; g < 8; ++g) prog.write(ins(0, g));
            prog.write(ins(1, 0));          // POP the input block
            prog.write(ins(2, 0));          // PUSH the output block
        }
    }
    w5_top(in, prog, out, status);
    int bad = 0, n = 0;
    for (int b = 0; b < jobs * blocks; ++b)
        for (int i = 0; i < 64; ++i) {
            const lg_t v = out.read();
            ++n;
            if (v != lg_t(1000 * b + i + 1)) ++bad;
        }
    for (int j = 0; j < jobs; ++j) {
        const word_t s = status.read();
        if (s != word_t(50 + j)) ++bad;
    }
    printf("WITNESS-TB5 words %d mismatches %d\n", n, bad);
    return bad ? 1 : 0;
}
