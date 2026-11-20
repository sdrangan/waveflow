// Drive the reference body, plain C++: read input words (one decimal per line) from argv[2],
// fire the body until s_in is empty, print "out <word> <last>" and "status <word> <last>".
// argv[1] is the word width, 32 or 64.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include "gain_stats_body.cpp"

template <int W>
int run(const char* path) {
    hls::stream<streamutils::axi4s_word<W>> s_in, m_out, m_status;
    std::ifstream f(path);
    unsigned long long v;
    while (f >> v) {
        streamutils::axi4s_word<W> w;
        w.data = v;
        w.last = 0;
        s_in.write(w);
    }
    while (!s_in.empty()) gain_stats<W>(s_in, m_out, m_status);
    while (!m_out.empty()) {
        auto w = m_out.read();
        // ap_axis data is a signed ap_int: print it as the unsigned word it is.
        std::printf("out %llu %d\n", (unsigned long long)ap_uint<W>(w.data).to_uint64(), (int)w.last);
    }
    while (!m_status.empty()) {
        auto w = m_status.read();
        std::printf("status %llu %d\n", (unsigned long long)ap_uint<W>(w.data).to_uint64(), (int)w.last);
    }
    return 0;
}

int main(int argc, char** argv) {
    if (argc != 3) return 2;
    return std::strcmp(argv[1], "64") == 0 ? run<64>(argv[2]) : run<32>(argv[2]);
}
