// fir_dflow_tb.cpp — self-contained testbench for the control-driven DATAFLOW
// FIR sandbox (plans/fir-cleanup.md §1 Phase 1).
//
// Scenario is selected by argv[1] (csim_design / cosim_design -argv convention):
//   single | two | three   — N back-to-back 4x64 FIR commands + END (no error).
//                            (single vs three give the per-command seam by diff;
//                             "three" is the back-to-back streaming reference.)
//   clean                   — 3 matrices of VARYING size + END (bit-exact sweep).
//   error                   — 2 good + 1 bad-size command -> kernel halts; then a
//                            RESTART feeds the remaining command + END and runs
//                            clean. Exercises the depth-1 err-stream + halt + restart.
//
// Data + golden are generated in C++ with the SAME left-to-right tap order as the
// kernel, so the gmem Y is compared BIT-EXACTLY (no float tolerance). Returns
// non-zero on any mismatch / control-state error so run_dflow.tcl reports
// WAVEFLOW_ERROR.
#include "fir_dflow_sandbox.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using fir_dflow::real_t;
using fir_dflow::T;
using fir_dflow::FIR_OP_FIR;
using fir_dflow::FIR_OP_END;
using fir_dflow::FIR_ERR_NONE;
using fir_dflow::FIR_ERR_BAD_SIZE;

namespace {

// Deterministic, small-magnitude, reproducible data (bit-exact golden meaningful).
real_t x_val(int idx) { return (real_t)((idx % 17) * 0.5f - 4.0f); }
real_t h_val(int t)   { return (real_t)((t + 1) * 0.25f - 1.0f); }

struct Mat {
    int n_rows, n_cols;
    unsigned tx_id;
    bool bad;            // inject an invalid size (error command)
    int x_off, h_off, y_off;   // assigned by layout
};

int out_len_of(int n_cols) { return n_cols - T + 1; }

// Lay matrices out contiguously in gmem (X | h | Y per matrix); fill X/h.
// A `bad` command reserves NO real space (its huge/invalid n_cols is the thing
// under test) — the kernel rejects it (n_rows->0) and never touches gmem, so its
// offsets just point at 0 (an in-range, harmless address for the unconditional
// tap read).
void layout_and_fill(std::vector<Mat>& mats, std::vector<real_t>& gmem) {
    int off = 0;
    int seed = 0;
    for (auto& m : mats) {
        if (m.bad) { m.x_off = m.h_off = m.y_off = 0; continue; }
        m.x_off = off;                          off += m.n_rows * m.n_cols;
        m.h_off = off;                          off += T;
        m.y_off = off;                          off += m.n_rows * out_len_of(m.n_cols);
        for (int i = 0; i < m.n_rows * m.n_cols; ++i) gmem[m.x_off + i] = x_val(seed + i);
        for (int t = 0; t < T; ++t)                   gmem[m.h_off + t] = h_val(t);
        seed += 13;
    }
    if (off > (int)gmem.size()) { std::fprintf(stderr, "layout overflow %d\n", off); std::exit(2); }
}

// Bit-exact reference (same tap order as the kernel's compute).
void golden_into(const std::vector<real_t>& gmem, const Mat& m, std::vector<real_t>& Yref) {
    const int ol = out_len_of(m.n_cols);
    Yref.assign((size_t)m.n_rows * ol, 0.0f);
    for (int r = 0; r < m.n_rows; ++r)
        for (int j = 0; j < ol; ++j) {
            real_t acc = 0;
            for (int t = 0; t < T; ++t)
                acc += gmem[m.h_off + t] * gmem[m.x_off + r * m.n_cols + j + (T - 1) - t];
            Yref[(size_t)r * ol + j] = acc;
        }
}

void push_cmd(hls::stream<ap_uint<32>>& s, const Mat& m, unsigned op) {
    s.write(op);
    s.write(m.tx_id);
    s.write((ap_uint<32>)m.x_off);
    s.write((ap_uint<32>)m.h_off);
    s.write((ap_uint<32>)m.y_off);
    s.write((ap_uint<32>)m.n_rows);
    s.write((ap_uint<32>)m.n_cols);
}
void push_end(hls::stream<ap_uint<32>>& s) {
    Mat z{}; z.tx_id = 0xFFFF; push_cmd(s, z, FIR_OP_END);
}

int check_Y(const std::vector<real_t>& gmem, const Mat& m, const char* tag) {
    std::vector<real_t> Yref; golden_into(gmem, m, Yref);
    const int ol = out_len_of(m.n_cols);
    int bad = 0;
    for (size_t i = 0; i < Yref.size(); ++i) {
        uint32_t a, b;
        std::memcpy(&a, &gmem[m.y_off + i], 4);
        std::memcpy(&b, &Yref[i], 4);
        if (a != b) {
            if (bad < 6)
                std::fprintf(stderr, "  [%s tx=%u] Y mismatch at %zu: got 0x%08x exp 0x%08x\n",
                             tag, m.tx_id, i, a, b);
            ++bad;
        }
    }
    if (bad == 0)
        std::printf("  [%s tx=%u %dx%d] Y bit-exact (%d elems)\n",
                    tag, m.tx_id, m.n_rows, m.n_cols, m.n_rows * ol);
    return bad;
}

int check_zero(const std::vector<real_t>& gmem, const Mat& m, const char* tag) {
    // A command that never ran (after a halt) must leave its Y region untouched (0).
    const int ol = out_len_of(m.n_cols);
    for (int i = 0; i < m.n_rows * ol; ++i)
        if (gmem[m.y_off + i] != 0.0f) {
            std::fprintf(stderr, "  [%s tx=%u] expected untouched Y, found nonzero at %d\n",
                         tag, m.tx_id, i);
            return 1;
        }
    std::printf("  [%s tx=%u] Y correctly untouched (kernel halted before it)\n", tag, m.tx_id);
    return 0;
}

void drain(hls::stream<ap_uint<32>>& m_out, int& n_resp) {
    n_resp = 0;
    while (!m_out.empty()) { m_out.read(); ++n_resp; }
}

}  // namespace

int main(int argc, char** argv) {
    const std::string scn = (argc > 1) ? argv[1] : "clean";
    std::vector<real_t> gmem(WF_FIR_GMEM_DEPTH, 0.0f);

    hls::stream<ap_uint<32>> s_in("s_in");
    hls::stream<ap_uint<32>> m_out("m_out");
    ap_uint<1>  halted = 0;
    ap_uint<8>  error  = 0;
    ap_uint<16> tx_id  = 0;

    int fails = 0;

    if (scn == "single" || scn == "two" || scn == "three") {
        const int n = (scn == "single") ? 1 : (scn == "two") ? 2 : 3;
        std::vector<Mat> mats;
        for (int i = 0; i < n; ++i) mats.push_back(Mat{4, 64, (unsigned)(100 + i), false, 0, 0, 0});
        layout_and_fill(mats, gmem);
        for (auto& m : mats) push_cmd(s_in, m, FIR_OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, halted, error, tx_id, gmem.data());

        int n_resp; drain(m_out, n_resp);
        for (auto& m : mats) fails += check_Y(gmem, m, scn.c_str());
        if (halted != 0) { std::fprintf(stderr, "  expected halted=0, got 1\n"); ++fails; }
        if (n_resp != 2 * n) { std::fprintf(stderr, "  expected %d resp words, got %d\n", 2*n, n_resp); ++fails; }
        std::printf("  scenario '%s': %d FIR cmd(s), halted=%d, resp_words=%d\n",
                    scn.c_str(), n, (int)halted, n_resp);

    } else if (scn == "clean") {
        std::vector<Mat> mats = {
            Mat{4, 64, 200, false, 0, 0, 0},
            Mat{2, 48, 201, false, 0, 0, 0},
            Mat{3, 32, 202, false, 0, 0, 0},
        };
        layout_and_fill(mats, gmem);
        for (auto& m : mats) push_cmd(s_in, m, FIR_OP_FIR);
        push_end(s_in);

        fir(s_in, m_out, halted, error, tx_id, gmem.data());

        int n_resp; drain(m_out, n_resp);
        for (auto& m : mats) fails += check_Y(gmem, m, "clean");
        if (halted != 0) { std::fprintf(stderr, "  expected halted=0, got 1\n"); ++fails; }

    } else if (scn == "error") {
        // 2 good, 1 bad-size (n_cols > NCOL_MAX); then a 4th good command that the
        // halt prevents from running in pass 1 and which the restart completes.
        std::vector<Mat> mats = {
            Mat{4, 64,   300, false, 0, 0, 0},
            Mat{4, 64,   301, false, 0, 0, 0},
            Mat{4, 4096, 302, true,  0, 0, 0},   // bad: n_cols > NCOL_MAX
            Mat{4, 64,   303, false, 0, 0, 0},
        };
        layout_and_fill(mats, gmem);

        // --- pass 1: feed [good, good, bad] with NO end -- the kernel halts on
        // the bad command and returns, consuming exactly those 3 headers (no
        // leftover words in s_in). The 4th command is NOT fed this pass.
        push_cmd(s_in, mats[0], FIR_OP_FIR);
        push_cmd(s_in, mats[1], FIR_OP_FIR);
        push_cmd(s_in, mats[2], FIR_OP_FIR);
        fir(s_in, m_out, halted, error, tx_id, gmem.data());
        int n_resp1; drain(m_out, n_resp1);

        std::printf("  [pass1] halted=%d error=%u tx_id=%u resp_words=%d\n",
                    (int)halted, (unsigned)error, (unsigned)tx_id, n_resp1);
        fails += check_Y(gmem, mats[0], "pass1");
        fails += check_Y(gmem, mats[1], "pass1");
        fails += check_zero(gmem, mats[3], "pass1");   // 4th never ran
        if (halted != 1) { std::fprintf(stderr, "  expected halted=1\n"); ++fails; }
        if (error != (ap_uint<8>)FIR_ERR_BAD_SIZE) { std::fprintf(stderr, "  expected error=BAD_SIZE\n"); ++fails; }
        if (tx_id != (ap_uint<16>)302) { std::fprintf(stderr, "  expected tx_id=302, got %u\n", (unsigned)tx_id); ++fails; }

        // --- restart: processor clears the halt regs and re-issues the remaining
        // work. s_in is empty (pass 1 left no leftovers), so this is a clean run.
        halted = 0; error = 0; tx_id = 0;
        push_cmd(s_in, mats[3], FIR_OP_FIR);
        push_end(s_in);
        fir(s_in, m_out, halted, error, tx_id, gmem.data());
        int n_resp2; drain(m_out, n_resp2);

        std::printf("  [pass2/restart] halted=%d resp_words=%d\n", (int)halted, n_resp2);
        fails += check_Y(gmem, mats[3], "pass2");      // now completed
        if (halted != 0) { std::fprintf(stderr, "  expected halted=0 after restart\n"); ++fails; }
        if (n_resp2 != 2) { std::fprintf(stderr, "  expected 2 resp words after restart, got %d\n", n_resp2); ++fails; }

    } else {
        std::fprintf(stderr, "unknown scenario '%s'\n", scn.c_str());
        return 2;
    }

    if (fails == 0) {
        std::printf("WAVEFLOW_FIR_DFLOW_OK: scenario '%s' passed (bit-exact + control state)\n",
                    scn.c_str());
        return 0;
    }
    std::fprintf(stderr, "WAVEFLOW_FIR_DFLOW_FAIL: scenario '%s' had %d failure(s)\n",
                 scn.c_str(), fails);
    return 1;
}
