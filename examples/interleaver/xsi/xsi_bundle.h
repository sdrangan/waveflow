#ifndef WAVEFLOW_XSI_BUNDLE_H
#define WAVEFLOW_XSI_BUNDLE_H
// xsi_bundle.h — C++ read/write for the burst-bundle test-vector format
// (waveflow.utils.burst_io): a directory holding words.bin + bounds.bin (both little-endian uint64)
// and a small meta.json.  This is the C++ half of the ONE on-disk format shared between the pysim
// StreamDriver/StreamSink (and the memory arena) and the XSI testbench — a bundle written by Python
// is read here, and a bundle written here is read back by Python, so neither side re-implements the
// vectors.  x86 is little-endian, so a raw fread/fwrite of uint64 matches numpy's "<u8".
//
// WIDE WORDS.  A beat wider than 64 bits is stored as k = ceil(W/64) consecutive uint64 CHUNKS,
// chunk 0 the low 64 bits -- the pysim's (n, k) uint64 convention for a wide stream, so the two
// sides agree on the bytes.  meta.json's word_bytes is 8*k (data, not convention), bounds.bin
// counts BEATS, and cycles.bin has one entry per beat.  k = 1 is every bundle written before this
// existed, byte for byte.
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

/// Where a testbench's bundle path *p* lives on disk.  Testbenches name their bundles
/// "vectors/<port>", relative to the run directory; when WF_VECTORS_DIR is set (run.bat / run.sh's
/// vectors-directory argument, plans/incremental_xsi.md D4) the leading "vectors" is replaced by
/// it, so two runs of one built testbench can read and write separate directories.  Unset, or a
/// path not under "vectors", is returned unchanged -- every run before this existed.
///
/// Resolved HERE, at the one place bundle files are opened, rather than in each generated main:
/// the hand-written and the generated testbenches then honour it alike, with no regeneration.
inline std::string vectors_path(const std::string& p) {
    const char* root = std::getenv("WF_VECTORS_DIR");
    if (!root || !*root) return p;
    if (p == "vectors") return root;
    if (p.size() >= 8 && p.compare(0, 7, "vectors") == 0 && (p[7] == '/' || p[7] == '\\'))
        return std::string(root) + p.substr(7);
    return p;
}

struct BurstBundle {
    /// The flat words of *dir*'s stream (words.bin): one AXIS beat each, or -- for a wide stream
    /// -- read_chunks(dir) uint64 chunks per beat, low chunk first.
    static std::vector<uint64_t> read_words(const std::string& dir) {
        return read_u64(dir + "/words.bin");
    }
    /// uint64 chunks per beat: meta.json's word_bytes / 8, or 1 when the manifest does not say.
    static int read_chunks(const std::string& dir) {
        const long wb = read_meta_int(dir, "word_bytes", 8);
        return wb > 8 ? (int)(wb / 8) : 1;
    }
    /// The integer value of *key* in meta.json, or *dflt* -- the unquoted-number twin of
    /// read_meta_str below, under the same minimal-scan contract.
    static long read_meta_int(const std::string& dir, const std::string& key, long dflt) {
        FILE* f = std::fopen(vectors_path(dir + "/meta.json").c_str(), "rb");
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
        return std::strtol(txt.c_str() + colon + 1, 0, 10);
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
    /// would be code to maintain against a format that cannot grow those shapes.
    ///
    /// It exists so a reader can REFUSE a bundle it would otherwise misread.  The RF bundle's
    /// element kind is the case: real and complex blocks are the same bytes at different lengths,
    /// so without the manifest a complex bundle read as real is not an error — it is a plausible
    /// wrong answer, which is worse.  Callers that need the key pass an empty *dflt* and treat the
    /// empty result as "this bundle does not say", which is now itself an error rather than a
    /// default — see rf_require_bundle_kind().
    static std::string read_meta_str(const std::string& dir, const std::string& key,
                                     const std::string& dflt = std::string()) {
        FILE* f = std::fopen(vectors_path(dir + "/meta.json").c_str(), "rb");
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
    ///
    /// *extra_key* / *extra_val* add ONE string entry to the manifest beside the four this struct
    /// owns.  **Pass-through, not interpretation** — the mirror of Python's
    /// `write_burst_bundle(..., extra=...)`, and for the same reason: this file knows nothing about
    /// RF, streams or memory arenas, and a writer that special-cased one of them here would be the
    /// place every future format learns about every other.
    ///
    /// It is a single pair rather than a map because there is exactly one such key today
    /// (`rf_element`), and a map would be machinery sized for a caller that does not exist.  A
    /// second key is a signature change, which is the right amount of friction for adding one.
    static void write(const std::string& dir,
                      const std::vector<uint64_t>& words,
                      const std::vector<uint64_t>& bounds,
                      const char* extra_key = 0, const char* extra_val = 0, int chunks = 1) {
        mkdirs(dir);
        write_u64(dir + "/words.bin", words);
        write_u64(dir + "/bounds.bin", bounds);
        const std::string meta = dir + "/meta.json";
        FILE* f = std::fopen(vectors_path(meta).c_str(), "wb");
        if (!f) die(meta);
        std::fprintf(f,
            "{\n  \"format\": \"waveflow.burst_bundle/1\",\n  \"word_bytes\": %d,\n"
            "  \"n_bursts\": %zu,\n  \"n_words\": %zu",
            8 * chunks, bounds.size(), words.size() / (size_t)chunks);
        if (extra_key && extra_val) std::fprintf(f, ",\n  \"%s\": \"%s\"", extra_key, extra_val);
        std::fprintf(f, "\n}\n");
        std::fclose(f);
    }

    /// Convenience: write a single-burst bundle (one burst spanning all of *words*) — e.g. a memory
    /// arena, or a continuous (has_tlast=false) stream.
    static void write_one(const std::string& dir, const std::vector<uint64_t>& words,
                          int chunks = 1) {
        std::vector<uint64_t> bounds(1, (uint64_t)(words.size() / (size_t)chunks));
        write(dir, words, bounds, 0, 0, chunks);
    }

    /// Write a captured output stream: the words bundle (words/bounds/meta) **plus** ``cycles.bin`` —
    /// the arrival cycle of each word (uint64, parallel to ``words``).  So the C++ side only records
    /// timing; Python reads ``cycles.bin`` and computes completion time (cycle_of_word) off-line.
    static void write_capture(const std::string& dir, const std::vector<uint64_t>& words,
                              const std::vector<long>& cycles, int chunks = 1) {
        write_one(dir, words, chunks);
        std::vector<uint64_t> c(cycles.begin(), cycles.end());
        write_u64(dir + "/cycles.bin", c);
    }

    /// The same capture, but with the FRAME BOUNDS the DUT actually asserted (one entry per TLAST
    /// beat) rather than a single burst spanning everything.
    ///
    /// A separate entry point rather than a defaulted argument: writing the true bounds is only
    /// possible when the port HAS a TLAST pin, and a default would quietly turn "this stream is not
    /// framed" into "this stream is one frame" — the same bytes, a different claim.  A caller that
    /// has bounds passes them; a caller that has none says so by calling the other function.
    static void write_capture(const std::string& dir, const std::vector<uint64_t>& words,
                              const std::vector<long>& cycles,
                              const std::vector<uint64_t>& bounds, int chunks = 1) {
        write(dir, words, bounds, 0, 0, chunks);
        std::vector<uint64_t> c(cycles.begin(), cycles.end());
        write_u64(dir + "/cycles.bin", c);
    }

private:
    /// Create *dir* and any missing parents (like Python's write_burst_bundle), ignoring
    /// already-exists.  No std::filesystem — it does not link with the run.bat mingw.
    static void mkdirs(const std::string& d) {
        const std::string dir = vectors_path(d);
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
        FILE* f = std::fopen(vectors_path(path).c_str(), "rb");
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
        FILE* f = std::fopen(vectors_path(path).c_str(), "wb");
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
