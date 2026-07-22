// fir_freerun_tb.cpp — self-contained testbench for the free-running streaming FIR
// sandbox (plans/fir_freerun_sandbox.md).
//
// Scenario via argv[1] (csim_design / cosim_design -argv):
//   single | two | three  — N back-to-back 4x64 jobs + END, ONE batch (one ap_start).
//                           single/two/three give the steady per-job period by diff.
//   clean                 — varying sizes 4x64, 2x48, 3x32 + END, one batch.
//   error                 — batch 1: [good, good, BAD-size, good] + END (the bad job
//                           gets an error status and the pipeline KEEPS FLOWING — the
//                           later good job still completes); batch 2 (RESTART): one
//                           good job + END.  Exercises per-job error + ap_done/restart.
//
// gmem is ap_uint<32>* (§2b); data is bit-cast float<->word.  Y is compared BIT-EXACT
// against a C++ golden in the same left-to-right tap order as the kernel.  Per-job
// responses {tx_id, status} are drained from m_out and checked.
#include "fir_freerun_sandbox.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using fir_freerun::T;
using fir_freerun::OP_FIR;
using fir_freerun::OP_END;
using fir_freerun::ST_OK;
using fir_freerun::ST_BAD_SIZE;
using fir_freerun::word_t;
using fir_freerun::bits_to_float;
using fir_freerun::float_to_bits;

namespace {

// Deterministic, small-magnitude, reproducible data (bit-exact golden meaningful).
float x_val(int idx) { return (float)((idx % 17) * 0.5f - 4.0f); }
float h_val(int t)   { return (float)((t + 1) * 0.25f - 1.0f); }

struct Job {
    int n_rows, n_cols;
    unsigned tx_id;
    bool bad;                       // inject an invalid size (per-job error)
    int x_off, h_off, y_off;        // assigned by layout
};

int out_len_of(int n_cols) { return n_cols - T + 1; }

// Lay jobs out contiguously in gmem (X | h | Y per job); fill X/h (bit-cast to words).
// A bad job reserves nothing (its offsets point at 0; the kernel never reads/writes it).
void layout_and_fill(std::vector<Job>& jobs, std::vector<ap_uint<32>>& gmem) {
    int off = 0, seed = 0;
    for (auto& j : jobs) {
        if (j.bad) { j.x_off = j.h_off = j.y_off = 0; continue; }
        j.x_off = off;                          off += j.n_rows * j.n_cols;
        j.h_off = off;                          off += T;
        j.y_off = off;                          off += j.n_rows * out_len_of(j.n_cols);
        for (int i = 0; i < j.n_rows * j.n_cols; ++i)
            gmem[j.x_off + i] = float_to_bits(x_val(seed + i));
        for (int t = 0; t < T; ++t)
            gmem[j.h_off + t] = float_to_bits(h_val(t));
        seed += 13;
    }
    if (off > (int)gmem.size()) { std::fprintf(stderr, "layout overflow %d\n", off); std::exit(2); }
}

// Bit-exact reference (same left-to-right tap order as the kernel's shift-register FIR).
void golden_into(const std::vector<ap_uint<32>>& gmem, const Job& j, std::vector<float>& Yref) {
    const int ol = out_len_of(j.n_cols);
    Yref.assign((size_t)j.n_rows * ol, 0.0f);
    for (int r = 0; r < j.n_rows; ++r)
        for (int c = 0; c < ol; ++c) {
            float acc = 0.0f;
            for (int t = 0; t < T; ++t)
                acc += bits_to_float(gmem[j.h_off + t]) *
                       bits_to_float(gmem[j.x_off + r * j.n_cols + c + (T - 1) - t]);
            Yref[(size_t)r * ol + c] = acc;
        }
}

void push_cmd(hls::stream<word_t>& s, const Job& j, unsigned op) {
    s.write((word_t)op);
    s.write((word_t)j.tx_id);
    s.write((word_t)j.x_off);
    s.write((word_t)j.h_off);
    s.write((word_t)j.y_off);
    s.write((word_t)j.n_rows);
    s.write((word_t)j.n_cols);
}
void push_end(hls::stream<word_t>& s) { Job z{}; push_cmd(s, z, OP_END); }

int check_Y(const std::vector<ap_uint<32>>& gmem, const Job& j, const char* tag) {
    std::vector<float> Yref; golden_into(gmem, j, Yref);
    const int ol = out_len_of(j.n_cols);
    int bad = 0;
    for (size_t i = 0; i < Yref.size(); ++i) {
        uint32_t a = (uint32_t)gmem[j.y_off + i];
        uint32_t b = (uint32_t)float_to_bits(Yref[i]);
        if (a != b) {
            if (bad < 6)
                std::fprintf(stderr, "  [%s tx=%u] Y mismatch at %zu: got 0x%08x exp 0x%08x\n",
                             tag, j.tx_id, i, a, b);
            ++bad;
        }
    }
    if (bad == 0)
        std::printf("  [%s tx=%u %dx%d] Y bit-exact (%d elems)\n",
                    tag, j.tx_id, j.n_rows, j.n_cols, j.n_rows * ol);
    return bad;
}

// Drain all responses currently in m_out as (tx_id, status) pairs.
std::vector<std::pair<unsigned, unsigned>> drain_resp(hls::stream<word_t>& m_out) {
    std::vector<std::pair<unsigned, unsigned>> r;
    while (!m_out.empty()) {
        unsigned tx = (unsigned)m_out.read();
        unsigned st = (unsigned)m_out.read();
        r.emplace_back(tx, st);
    }
    return r;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string scn = (argc > 1) ? argv[1] : "clean";
    std::vector<ap_uint<32>> gmem(WF_FIR_FR_GMEM_DEPTH, ap_uint<32>(0));

    hls::stream<word_t> s_in("s_in");
    hls::stream<word_t> m_out("m_out");
    int fails = 0;

    if (scn == "single" || scn == "two" || scn == "three") {
        const int n = (scn == "single") ? 1 : (scn == "two") ? 2 : 3;
        std::vector<Job> jobs;
        for (int i = 0; i < n; ++i) jobs.push_back(Job{4, 64, (unsigned)(100 + i), false, 0, 0, 0});
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, gmem.data());

        for (auto& j : jobs) fails += check_Y(gmem, j, scn.c_str());
        auto resp = drain_resp(m_out);
        if ((int)resp.size() != n) { std::fprintf(stderr, "  expected %d resp, got %zu\n", n, resp.size()); ++fails; }
        for (auto& rp : resp) if (rp.second != ST_OK) { std::fprintf(stderr, "  tx=%u status=%u (!=OK)\n", rp.first, rp.second); ++fails; }
        std::printf("  scenario '%s': %d job(s), %zu resp, all OK\n", scn.c_str(), n, resp.size());

    } else if (scn == "clean") {
        std::vector<Job> jobs = {
            Job{4, 64, 200, false, 0, 0, 0},
            Job{2, 48, 201, false, 0, 0, 0},
            Job{3, 32, 202, false, 0, 0, 0},
        };
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, gmem.data());

        for (auto& j : jobs) fails += check_Y(gmem, j, "clean");
        auto resp = drain_resp(m_out);
        if (resp.size() != jobs.size()) { std::fprintf(stderr, "  expected %zu resp, got %zu\n", jobs.size(), resp.size()); ++fails; }
        for (auto& rp : resp) if (rp.second != ST_OK) { std::fprintf(stderr, "  tx=%u status=%u (!=OK)\n", rp.first, rp.second); ++fails; }

    } else if (scn == "error") {
        // --- batch 1: good, good, BAD-size, good (the bad job does NOT halt the pipe) ---
        std::vector<Job> jobs = {
            Job{4, 64,   300, false, 0, 0, 0},
            Job{4, 64,   301, false, 0, 0, 0},
            Job{4, 4096, 302, true,  0, 0, 0},   // bad: n_cols > NCOL_MAX -> error status
            Job{4, 64,   303, false, 0, 0, 0},   // STILL completes (no global halt)
        };
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, gmem.data());

        fails += check_Y(gmem, jobs[0], "batch1");
        fails += check_Y(gmem, jobs[1], "batch1");
        fails += check_Y(gmem, jobs[3], "batch1");   // the post-error good job still ran
        auto resp = drain_resp(m_out);
        // Expect 4 responses, in order, with the bad one flagged and the rest OK.
        const unsigned want_tx[4] = {300, 301, 302, 303};
        const unsigned want_st[4] = {ST_OK, ST_OK, ST_BAD_SIZE, ST_OK};
        if (resp.size() != 4) { std::fprintf(stderr, "  batch1 expected 4 resp, got %zu\n", resp.size()); ++fails; }
        for (size_t i = 0; i < resp.size() && i < 4; ++i) {
            if (resp[i].first != want_tx[i] || resp[i].second != want_st[i]) {
                std::fprintf(stderr, "  batch1 resp[%zu] = (tx=%u,st=%u) expected (tx=%u,st=%u)\n",
                             i, resp[i].first, resp[i].second, want_tx[i], want_st[i]);
                ++fails;
            }
        }
        std::printf("  [batch1] %zu resp; bad job tx=302 flagged, tx=303 still completed\n", resp.size());

        // --- batch 2 (RESTART): a second ap_start, one good job ---
        Job j2{4, 64, 400, false, 0, 0, 0};
        std::vector<Job> b2 = {j2};
        layout_and_fill(b2, gmem);
        push_cmd(s_in, b2[0], OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, gmem.data());

        fails += check_Y(gmem, b2[0], "batch2");
        auto resp2 = drain_resp(m_out);
        if (resp2.size() != 1 || resp2[0].second != ST_OK) { std::fprintf(stderr, "  batch2 restart resp wrong\n"); ++fails; }
        std::printf("  [batch2/restart] %zu resp, OK\n", resp2.size());

    } else {
        std::fprintf(stderr, "unknown scenario '%s'\n", scn.c_str());
        return 2;
    }

    if (fails == 0) {
        std::printf("WAVEFLOW_FIR_FREERUN_OK: scenario '%s' passed (bit-exact + per-job resp)\n", scn.c_str());
        return 0;
    }
    std::fprintf(stderr, "WAVEFLOW_FIR_FREERUN_FAIL: scenario '%s' had %d failure(s)\n", scn.c_str(), fails);
    return 1;
}
