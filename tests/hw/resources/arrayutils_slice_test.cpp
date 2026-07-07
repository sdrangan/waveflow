// Phase 1b range-method (read_array_slice / write_array_slice) conformance testbench (template).
//
// Three checks per case, against the Python golden (arrayutils.write_array):
//   (A) static whole-array overloads: read [0, N) then re-pack must reproduce the input words.
//   (B) ranged read  [I0, I1): read the sub-range, re-pack contiguously -> out_read file,
//       compared to write_array(data[I0:I1]).
//   (C) ranged write [I0, I1) RMW: copy the full golden words, write a replacement sub-array into
//       the middle -> out_write file, compared to write_array(data with [I0:I1] replaced). This
//       is the load-bearing check: neighbor elements sharing the boundary words must be preserved.
#include <fstream>
#include <iostream>
#include <vector>

#include "__HEADER__"

namespace au = __NAMESPACE__;

static std::vector<ap_uint<__WORD_BW__>> read_words(const char* path) {
    std::ifstream f(path);
    std::vector<ap_uint<__WORD_BW__>> w;
    unsigned long long raw = 0;
    while (f >> raw) {
        w.push_back((ap_uint<__WORD_BW__>)raw);
    }
    return w;
}

static void dump_words(const char* path, const ap_uint<__WORD_BW__>* w, int n) {
    std::ofstream o(path);
    for (int i = 0; i < n; ++i) {
        o << static_cast<unsigned long long>(w[i]) << "\n";
    }
}

int main(int argc, char** argv) {
    const char* in_full = (argc > 1) ? argv[1] : "in_full.txt";
    const char* in_repl = (argc > 2) ? argv[2] : "in_repl.txt";
    const char* out_read = (argc > 3) ? argv[3] : "out_read.txt";
    const char* out_write = (argc > 4) ? argv[4] : "out_write.txt";

    constexpr int N = __N__;
    constexpr int M = __M__;
    constexpr int I0 = __I0__;
    constexpr int I1 = __I1__;
    constexpr int NWF = __NWORDS_FULL__;
    constexpr int NWS = __NWORDS_SUB__;

    std::vector<ap_uint<__WORD_BW__>> full_v = read_words(in_full);
    std::vector<ap_uint<__WORD_BW__>> repl_v = read_words(in_repl);
    if ((int)full_v.size() != NWF || (int)repl_v.size() != NWS) {
        std::cerr << "Unexpected word counts: full=" << full_v.size() << "/" << NWF
                  << " repl=" << repl_v.size() << "/" << NWS << std::endl;
        return 1;
    }
    ap_uint<__WORD_BW__> full[NWF];
    for (int i = 0; i < NWF; ++i) full[i] = full_v[i];
    ap_uint<__WORD_BW__> repl[NWS];
    for (int i = 0; i < NWS; ++i) repl[i] = repl_v[i];

    // (A) static whole-array overloads: read [0,N) and re-pack -> must equal the input words.
    au::value_type whole[N];
    au::read_array_slice<__WORD_BW__>(full, whole);          // 2-arg overload, N deduced
    ap_uint<__WORD_BW__> whole_w[NWF];
    for (int i = 0; i < NWF; ++i) whole_w[i] = 0;
    au::write_array_slice<__WORD_BW__>(whole, whole_w);       // 2-arg overload, N deduced
    for (int i = 0; i < NWF; ++i) {
        if (whole_w[i] != full[i]) {
            std::cerr << "Whole-array overload mismatch at word " << i << std::endl;
            return 5;
        }
    }

    // (B) ranged read [I0, I1) -> re-pack contiguously (range [0, M)) -> out_read.
    au::value_type rbuf[M];
    au::read_array_slice<__WORD_BW__>(full, I0, I1, rbuf);
    ap_uint<__WORD_BW__> rout[NWS];
    for (int i = 0; i < NWS; ++i) rout[i] = 0;                // fresh buffer: pad bits stay 0
    au::write_array_slice<__WORD_BW__>(rbuf, rout, 0, M);
    dump_words(out_read, rout, NWS);

    // (C) ranged write [I0, I1) RMW: start from a copy of the full golden, splice in M
    // replacement elements, dump the whole array. Neighbors in the boundary words must survive --
    // this is the read-modify-write variant (write_array_slice itself is pure-write and would
    // clobber the boundary-word neighbors).
    ap_uint<__WORD_BW__> work[NWF];
    for (int i = 0; i < NWF; ++i) work[i] = full[i];
    au::value_type wbuf[M];
    au::read_array_slice<__WORD_BW__>(repl, 0, M, wbuf);
    au::write_array_slice_rmw<__WORD_BW__>(wbuf, work, I0, I1);
    dump_words(out_write, work, NWF);

    return 0;
}
