// The logic check of os_pre_task.h's loop body (os_pre_body::step), a cycle at a time, under g++ with
// stand-in headers (tests/examples/test_os_filterbank.py).  stdin: one event a line, in cycle order --
//   <cycle> sel <v>        the arbiter's index word, readable from <cycle>
//   <cycle> tag <v>        a frame's tag word
//   <cycle> w <v>          a sample word (NW of them a frame, the last with TLAST)
// then `run <n>`.  stdout: `<cycle> x <v>`, `<cycle> dir <v>`, `<cycle> tag <v>` -- each word as the body
// writes it, on the cycle it is written.  A read of an empty stream aborts (the stand-in hls_stream.h).
#include <cstdio>
#include <cstring>
#include <vector>
#include "streamutils_hls.h"
#include "os_filterbank_config.h"
#include "tag.h"
#include "../../../examples/os_filterbank/src/os_pre_task.h"

#ifndef NW_
#define NW_ 16
#endif

typedef streamutils::framed_word<OS_W_LOOP> fw;
struct Ev { long c; int kind; unsigned long long v; };

int main() {
    std::vector<Ev> evs;
    long n = 0;
    char op[16];
    long c;
    while (std::scanf("%15s", op) == 1) {
        if (!std::strcmp(op, "run")) { std::scanf("%ld", &n); break; }
        c = std::atol(op);
        unsigned long long v;
        std::scanf("%15s %llu", op, &v);
        evs.push_back({c, !std::strcmp(op, "sel") ? 0 : !std::strcmp(op, "tag") ? 1 : 2, v});
    }
    hls::stream<fw> s_frm, s_sel, m_tag;
    hls::stream<ap_uint<OS_W_LOOP> > m_x;
    hls::stream<ap_uint<8> > m_dir;
    size_t e = 0;
    int nw = 0;
    for (long cyc = 0; cyc < n; ++cyc) {
        for (; e < evs.size() && evs[e].c <= cyc; ++e) {
            const Ev& ev = evs[e];
            if (ev.kind == 0) streamutils::write_boundary_word<fw, OS_W_LOOP>(s_sel, ap_uint<OS_W_LOOP>(ev.v), true);
            else if (ev.kind == 1) streamutils::write_boundary_word<fw, OS_W_LOOP>(s_frm, ap_uint<OS_W_LOOP>(ev.v), true);
            else {
                nw = (nw + 1) % NW_;
                streamutils::write_boundary_word<fw, OS_W_LOOP>(s_frm, ap_uint<OS_W_LOOP>(ev.v), nw == 0);
            }
        }
        os_pre_body::step<NW_>(s_frm, s_sel, m_x, m_dir, m_tag);
        while (!m_x.empty()) std::printf("%ld x %llu\n", cyc, (unsigned long long)m_x.read());
        while (!m_dir.empty()) std::printf("%ld dir %llu\n", cyc, (unsigned long long)m_dir.read());
        while (!m_tag.empty()) std::printf("%ld tag %llu\n", cyc, (unsigned long long)m_tag.read().data);
    }
    return 0;
}
