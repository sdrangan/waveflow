#ifndef WAVEFLOW_XSI_BUNDLE_H
#define WAVEFLOW_XSI_BUNDLE_H
// xsi_bundle.h — C++ read/write for the burst-bundle test-vector format
// (waveflow.utils.burst_io): a directory holding words.bin + bounds.bin (both little-endian uint64)
// and a small meta.json.  This is the C++ half of the ONE on-disk format shared between the pysim
// StreamDriver/StreamSink (and the memory arena) and the XSI testbench — a bundle written by Python
// is read here, and a bundle written here is read back by Python, so neither side re-implements the
// vectors.  x86 is little-endian, so a raw fread/fwrite of uint64 matches numpy's "<u8".
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#ifdef _WIN32
#include <direct.h>     // _mkdir  (std::filesystem does not link with the run.bat mingw)
#else
#include <sys/stat.h>   // mkdir
#endif

namespace wfbfm {

struct BurstBundle {
    /// The flat words of *dir*'s stream (words.bin), one AXIS beat each.
    static std::vector<uint64_t> read_words(const std::string& dir) {
        return read_u64(dir + "/words.bin");
    }
    /// The cumulative burst end-indices (bounds.bin); burst k is words[bounds[k-1]:bounds[k]].
    static std::vector<uint64_t> read_bounds(const std::string& dir) {
        return read_u64(dir + "/bounds.bin");
    }

    /// The string value of *key* in meta.json, or *dflt* when the file or the key is absent.
    ///
    /// A deliberately minimal scan, not a JSON parser: it finds `"key"`, the next `:`, and the
    /// quoted token after it.  The manifest is machine-written by one function
    /// (waveflow.utils.burst_io.write_burst_bundle) with no nesting and no escapes, so a parser
    /// would be code to maintain against a format that cannot grow those shapes.  Anything it
    /// cannot read comes back as *dflt*, which is what an absent key means anyway.
    ///
    /// It exists so a reader can REFUSE a bundle it would otherwise misread.  The RF bundle's
    /// element kind is the case: real and complex blocks are the same bytes at different lengths,
    /// so without the manifest a complex bundle read as real is not an error — it is a plausible
    /// wrong answer, which is worse.
    static std::string read_meta_str(const std::string& dir, const std::string& key,
                                     const std::string& dflt = std::string()) {
        FILE* f = std::fopen((dir + "/meta.json").c_str(), "rb");
        if (!f) return dflt;
        std::string txt;
        char buf[512];
        size_t n;
        while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) txt.append(buf, n);
        std::fclose(f);

        const std::string pat = "\"" + key + "\"";
        const size_t k = txt.find(pat);
        if (k == std::string::npos) return dflt;
        const size_t colon = txt.find(':', k + pat.size());
        if (colon == std::string::npos) return dflt;
        const size_t q0 = txt.find('"', colon + 1);
        if (q0 == std::string::npos) return dflt;
        const size_t q1 = txt.find('"', q0 + 1);
        if (q1 == std::string::npos) return dflt;
        return txt.substr(q0 + 1, q1 - q0 - 1);
    }

    /// Write words + bounds + a minimal meta.json into *dir* (which must already exist).  Matches
    /// what waveflow.utils.burst_io.write_burst_bundle produces, so read_burst_bundle validates it.
    static void write(const std::string& dir,
                      const std::vector<uint64_t>& words,
                      const std::vector<uint64_t>& bounds) {
        mkdirs(dir);
        write_u64(dir + "/words.bin", words);
        write_u64(dir + "/bounds.bin", bounds);
        const std::string meta = dir + "/meta.json";
        FILE* f = std::fopen(meta.c_str(), "wb");
        if (!f) die(meta);
        std::fprintf(f,
            "{\n  \"format\": \"waveflow.burst_bundle/1\",\n  \"word_bytes\": 8,\n"
            "  \"n_bursts\": %zu,\n  \"n_words\": %zu\n}\n",
            bounds.size(), words.size());
        std::fclose(f);
    }

    /// Convenience: write a single-burst bundle (one burst spanning all of *words*) — e.g. a memory
    /// arena, or a continuous (has_tlast=false) stream.
    static void write_one(const std::string& dir, const std::vector<uint64_t>& words) {
        std::vector<uint64_t> bounds(1, (uint64_t)words.size());
        write(dir, words, bounds);
    }

    /// Write a captured output stream: the words bundle (words/bounds/meta) **plus** ``cycles.bin`` —
    /// the arrival cycle of each word (uint64, parallel to ``words``).  So the C++ side only records
    /// timing; Python reads ``cycles.bin`` and computes completion time (cycle_of_word) off-line.
    static void write_capture(const std::string& dir, const std::vector<uint64_t>& words,
                              const std::vector<long>& cycles) {
        write_one(dir, words);
        std::vector<uint64_t> c(cycles.begin(), cycles.end());
        write_u64(dir + "/cycles.bin", c);
    }

private:
    /// Create *dir* and any missing parents (like Python's write_burst_bundle), ignoring
    /// already-exists.  No std::filesystem — it does not link with the run.bat mingw.
    static void mkdirs(const std::string& dir) {
        for (size_t i = 1; i <= dir.size(); ++i) {
            if (i == dir.size() || dir[i] == '/' || dir[i] == '\\') {
                std::string sub = dir.substr(0, i);
#ifdef _WIN32
                _mkdir(sub.c_str());
#else
                mkdir(sub.c_str(), 0777);
#endif
            }
        }
    }

    static std::vector<uint64_t> read_u64(const std::string& path) {
        FILE* f = std::fopen(path.c_str(), "rb");
        if (!f) die(path);
        std::fseek(f, 0, SEEK_END);
        long n = std::ftell(f);
        std::fseek(f, 0, SEEK_SET);
        std::vector<uint64_t> v(n > 0 ? (size_t)n / 8 : 0);
        if (!v.empty() && std::fread(v.data(), 8, v.size(), f) != v.size()) die(path);
        std::fclose(f);
        return v;
    }
    static void write_u64(const std::string& path, const std::vector<uint64_t>& v) {
        FILE* f = std::fopen(path.c_str(), "wb");
        if (!f) die(path);
        if (!v.empty() && std::fwrite(v.data(), 8, v.size(), f) != v.size()) die(path);
        std::fclose(f);
    }
    static void die(const std::string& path) {
        std::fprintf(stderr, "FATAL: burst-bundle I/O failed: %s\n", path.c_str());
        std::exit(4);
    }
};

}  // namespace wfbfm
#endif  // WAVEFLOW_XSI_BUNDLE_H
