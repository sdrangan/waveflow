// Phase 3 elem-method conformance testbench (template).
//
// Exercises the random-access element primitives elem_read<W> / elem_write<W> (iw = i/LW,
// k = i%LW; reuse the shared run_lane / write_lane packing contract).  Two checks:
//
//   (1) elem_read(pack(v), i) == v[i]  — random single-element read equals the lane-decoded golden.
//   (2) elem_write builds the packed words back element-by-element (lane RMW); the re-emitted words
//       are written to the out file and compared, bit-exact, to the Python golden by the driver.
//
// Any in-sim divergence (check 1) returns non-zero so the csim (and the test) fails.  Reuses the
// tests/hw/test_arrayutils_vitis.py harness (template cpp + tcl + csim).
#include <fstream>
#include <iostream>
#include <vector>

#include <hls_stream.h>

#include "__HEADER__"

namespace au = __NAMESPACE__;

// Reference decode: all N elements via the lane loop (running word pointer advanced by WPU).
template <int W>
static void mem_lane_read(const ap_uint<W>* words, au::value_type* dst, int N) {
    constexpr int LW = au::lane_capacity<W>();
    constexpr int WPU = au::get_nwords<W>(LW);
    const ap_uint<W>* xp = words;
    for (int i = 0; i < N; i += LW) {
        const int n = (N - i < LW) ? (N - i) : LW;
        au::value_type lane[LW];
        au::read_array_lane<W>(xp, lane, n);
        for (int k = 0; k < LW; ++k) {
            if (k < n) dst[i + k] = lane[k];
        }
        xp += WPU;
    }
}

int main(int argc, char** argv) {
    const char* in_words_path = (argc > 1) ? argv[1] : "array_words.txt";
    const char* out_words_path = (argc > 2) ? argv[2] : "array_words_out.txt";

    std::ifstream in_words(in_words_path);
    if (!in_words) {
        std::cerr << "Failed to open input words file: " << in_words_path << std::endl;
        return 1;
    }

    std::vector<ap_uint<__WORD_BW__>> words;
    unsigned long long raw = 0;
    while (in_words >> raw) {
        words.push_back((ap_uint<__WORD_BW__>)raw);
    }
    if ((int)words.size() != __NWORDS__) {
        std::cerr << "Unexpected word count: got " << words.size()
                  << ", expected " << __NWORDS__ << std::endl;
        return 1;
    }

    constexpr int N = __ARRAY_LEN__;

    // Golden elements: lane-decode the packed words.
    au::value_type elems[N];
    mem_lane_read<__WORD_BW__>(words.data(), elems, N);

    // (1) elem_read: random single-element read must equal the lane-decoded golden, every index.
    for (int i = 0; i < N; ++i) {
        au::value_type v = au::elem_read<__WORD_BW__>(words.data(), i);
        if (v != elems[i]) {
            std::cerr << "elem_read mismatch at index " << i << std::endl;
            return 2;
        }
    }

    // (2) elem_write: rebuild the packed words element-by-element (lane RMW); compared to the golden.
    ap_uint<__WORD_BW__> out_words[__NWORDS__];
    for (int w = 0; w < __NWORDS__; ++w) out_words[w] = 0;
    for (int i = 0; i < N; ++i) {
        au::elem_write<__WORD_BW__>(elems[i], out_words, i);
    }

    std::ofstream out(out_words_path);
    if (!out) {
        std::cerr << "Failed to open output words file: " << out_words_path << std::endl;
        return 1;
    }
    for (int i = 0; i < __NWORDS__; ++i) {
        out << static_cast<unsigned long long>(out_words[i]) << "\n";
    }
    return 0;
}
