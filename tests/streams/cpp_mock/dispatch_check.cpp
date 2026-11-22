// g++ check of stream_dispatch_task's logic against a script (tests/streams/test_dispatch.py).  Reads
// lines from stdin: "job <n0> [<n1> ...]" (one job, its bursts' lengths in words), "credit <k>" (a word
// k on s_free) and "fire <n>" (call the body n times).  Prints "sent <worker> <job>" for each job as its
// last burst reaches an output, and at the end "left <s_in words> <s_free words>".  Three outputs; the
// burst count NB_, the credits C_ and the policy RR_ are macros.
#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include "stream_dispatch_task.h"
#ifndef NB_
#define NB_ 1
#endif
#ifndef C_
#define C_ 1
#endif
#ifndef RR_
#define RR_ 0
#endif
typedef streamutils::framed_word<32> wt;
int main() {
    hls::stream<wt> s_in, s_free, o[3];
    unsigned bursts[3] = {0, 0, 0};
    unsigned job = 0;
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream is(line);
        std::string op;
        is >> op;
        if (op == "job") {
            int n;
            unsigned b = 0;
            while (is >> n) {
                for (int j = 0; j < n; ++j) {
                    wt w;
                    w.data = ap_uint<32>((job << 12) | (b << 8) | unsigned(j));
                    w.last = (j == n - 1);
                    s_in.write(w);
                }
                ++b;
            }
            ++job;
        } else if (op == "credit") {
            int k;
            is >> k;
            wt w;
            w.data = ap_uint<32>(k);
            w.last = true;
            s_free.write(w);
        } else if (op == "fire") {
            int n;
            is >> n;
            for (int i = 0; i < n; ++i) {
                stream_dispatch_task<32, NB_, C_, RR_, wt, wt, wt, wt, wt>(s_in, s_free, o[0], o[1], o[2]);
                for (int k = 0; k < 3; ++k) {
                    while (!o[k].empty()) {
                        const wt w = o[k].read();
                        if (w.last && ++bursts[k] % NB_ == 0)
                            std::printf("sent %d %u\n", k, unsigned((unsigned long long)w.data >> 12));
                    }
                }
            }
        }
    }
    std::printf("left %zu %zu\n", s_in.size(), s_free.size());
    return 0;
}
