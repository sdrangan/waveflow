#pragma once
// bundle_tb.h -- TESTBENCH ONLY (not synthesizable): stream stimulus and capture as files.
//
// A hand-written C++ testbench and the Python model read the SAME stimulus, written once
// by Python as a burst bundle (waveflow/utils/burst_io.py, format waveflow.burst_bundle/1):
//
//   <dir>/words.bin   every word, burst after burst, little-endian uint64
//   <dir>/bounds.bin  the cumulative end word-index of each burst, uint64
//   <dir>/tlast.bin   one uint8 per burst: 1 = TLAST on its last word, 0 = no TLAST
//                     (optional on read: absent means every burst ends with TLAST)
//   <dir>/meta.json   format, word_bytes, n_bursts, n_words
//
//   wf::play_stream<W>(dir, s)    push every burst of a bundle into a stream
//   wf::record_stream<W>(s, dir)  drain a stream into a bundle
//
// A burst with tlast=0 is how a testbench sends a malformed transaction (a missing TLAST).
// Note what the wire can carry: TLAST is the ONLY burst boundary, so a recorded stream splits
// at TLAST and nowhere else.  A no-TLAST burst followed by another burst is recorded as one
// merged burst -- which is exactly what the kernel saw.  A recording that ends without TLAST
// gives a final burst with tlast=0.
//
// Words are stored as uint64, so stream widths up to 64 bits are supported.  The bundle
// directory must exist before record_stream writes to it (create it from Python, or from the
// build step that runs the testbench).

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "streamutils_hls.h"

namespace wf {

struct Burst {
    std::vector<uint64_t> words;
    bool tlast = true;
};

inline std::vector<uint64_t> read_u64_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("bundle_tb: cannot open " + path);
    std::vector<uint64_t> out;
    uint64_t v;
    while (f.read(reinterpret_cast<char*>(&v), sizeof v)) out.push_back(v);
    return out;
}

// Read every burst of a bundle.  Checks bounds against the word count.
inline std::vector<Burst> read_bundle(const std::string& dir) {
    const std::vector<uint64_t> words = read_u64_file(dir + "/words.bin");
    const std::vector<uint64_t> bounds = read_u64_file(dir + "/bounds.bin");
    std::vector<uint8_t> tlast(bounds.size(), 1);
    std::ifstream tf(dir + "/tlast.bin", std::ios::binary);
    if (tf) {
        for (size_t k = 0; k < bounds.size(); ++k) {
            char c = 1;
            if (!tf.get(c)) throw std::runtime_error("bundle_tb: tlast.bin shorter than bounds.bin in " + dir);
            tlast[k] = static_cast<uint8_t>(c);
        }
    }
    std::vector<Burst> out;
    uint64_t prev = 0;
    for (size_t k = 0; k < bounds.size(); ++k) {
        if (bounds[k] < prev || bounds[k] > words.size())
            throw std::runtime_error("bundle_tb: bad bounds.bin in " + dir);
        Burst b;
        b.words.assign(words.begin() + prev, words.begin() + bounds[k]);
        b.tlast = tlast[k] != 0;
        out.push_back(b);
        prev = bounds[k];
    }
    if (prev != words.size())
        throw std::runtime_error("bundle_tb: final bound != word count in " + dir);
    return out;
}

// Write bursts as a bundle into an EXISTING directory.
inline void write_bundle(const std::string& dir, const std::vector<Burst>& bursts) {
    std::ofstream wf_(dir + "/words.bin", std::ios::binary);
    std::ofstream bf(dir + "/bounds.bin", std::ios::binary);
    std::ofstream tf(dir + "/tlast.bin", std::ios::binary);
    if (!wf_ || !bf || !tf)
        throw std::runtime_error("bundle_tb: cannot write a bundle in " + dir +
                                 " (does the directory exist?)");
    uint64_t end = 0;
    for (const Burst& b : bursts) {
        for (uint64_t w : b.words) wf_.write(reinterpret_cast<const char*>(&w), sizeof w);
        end += b.words.size();
        bf.write(reinterpret_cast<const char*>(&end), sizeof end);
        const char t = b.tlast ? 1 : 0;
        tf.write(&t, 1);
    }
    std::ofstream mf(dir + "/meta.json");
    mf << "{\n  \"format\": \"waveflow.burst_bundle/1\",\n  \"word_bytes\": 8,\n"
       << "  \"n_bursts\": " << bursts.size() << ",\n  \"n_words\": " << end << "\n}\n";
}

// Push every burst of the bundle in `dir` into `s`.  Returns the number of words pushed.
template <int W>
inline int play_stream(const std::string& dir, hls::stream<streamutils::axi4s_word<W>>& s) {
    static_assert(W <= 64, "bundle_tb: bundles hold uint64 words; streams wider than 64 bits are not supported");
    int n = 0;
    for (const Burst& b : read_bundle(dir)) {
        for (size_t i = 0; i < b.words.size(); ++i) {
            streamutils::axi4s_word<W> w;
            w.data = ap_uint<W>(b.words[i]);
            w.last = (i + 1 == b.words.size()) && b.tlast;
            w.keep = -1;
            w.strb = -1;
            s.write(w);
            ++n;
        }
    }
    return n;
}

// Drain `s` into a bundle in the existing directory `dir`, splitting at TLAST.
// Returns the number of words recorded.
template <int W>
inline int record_stream(hls::stream<streamutils::axi4s_word<W>>& s, const std::string& dir) {
    static_assert(W <= 64, "bundle_tb: bundles hold uint64 words; streams wider than 64 bits are not supported");
    std::vector<Burst> out;
    Burst cur;
    int n = 0;
    while (!s.empty()) {
        streamutils::axi4s_word<W> w = s.read();
        // Mask to W bits: widening the data field to 64 bits sign-extends a word whose top
        // bit is set (measured: a negative float32, 0xc15c40db, came back 0xffffffffc15c40db).
        uint64_t v = static_cast<uint64_t>(w.data.to_uint64());
        if (W < 64) v &= (uint64_t(1) << W) - 1;
        cur.words.push_back(v);
        ++n;
        if (w.last) {
            cur.tlast = true;
            out.push_back(cur);
            cur = Burst();
        }
    }
    if (!cur.words.empty()) {
        cur.tlast = false;
        out.push_back(cur);
    }
    write_bundle(dir, out);
    return n;
}

}  // namespace wf
