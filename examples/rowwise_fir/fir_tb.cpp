// fir_tb.cpp — testbench for the GENERATED free-running streaming FIR top (Stage A).
//
// Drives gen/fir.cpp's `fir(s_in, m_out, gmem)` (ap_ctrl_hs + axis s_in/m_out + m_axi
// ap_uint<32>* gmem).  Commands are pushed via the built-in FIRCmd.write_axi4_stream<32>;
// per-job responses read via FIRResp.read_axi4_stream<32>; gmem is ap_uint<32> with float
// bit-cast.  Y is compared BIT-EXACT to a C++ golden in the same left-to-right tap order.
//
// argv[1] scenario: single|two|three (N×4×64) | clean (varying) | error (per-job error +
// restart).  Reproduces the sandbox gates against the generated kernel.
#include "gen/fir.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

static const int T = 8;
// gmem array size; the generated top's m_axi `depth=m_mem_depth` is patched to match this in
// run_fir.py (codegen can't infer the hook's opaque gmem extent, so it defaults to 1).
static const int GMEM_DEPTH = 8192;

float x_val(int idx) { return (float)((idx % 17) * 0.5f - 4.0f); }
float h_val(int t)   { return (float)((t + 1) * 0.25f - 1.0f); }

float u2f(ap_uint<32> u) { union { uint32_t i; float f; } c; c.i = (uint32_t)u; return c.f; }
ap_uint<32> f2u(float f) { union { uint32_t i; float f; } c; c.f = f; return ap_uint<32>(c.i); }

struct Job { int n_rows, n_cols; unsigned tx_id; bool bad; int x_off, h_off, y_off; };

int out_len_of(int n_cols) { return n_cols - T + 1; }

void layout_and_fill(std::vector<Job>& jobs, std::vector<ap_uint<32>>& gmem) {
    int off = 0, seed = 0;
    for (auto& j : jobs) {
        if (j.bad) { j.x_off = j.h_off = j.y_off = 0; continue; }
        j.x_off = off;                          off += j.n_rows * j.n_cols;
        j.h_off = off;                          off += T;
        j.y_off = off;                          off += j.n_rows * out_len_of(j.n_cols);
        for (int i = 0; i < j.n_rows * j.n_cols; ++i) gmem[j.x_off + i] = f2u(x_val(seed + i));
        for (int t = 0; t < T; ++t)                   gmem[j.h_off + t] = f2u(h_val(t));
        seed += 13;
    }
    if (off > (int)gmem.size()) { std::fprintf(stderr, "layout overflow %d\n", off); std::exit(2); }
}

void golden_into(const std::vector<ap_uint<32>>& gmem, const Job& j, std::vector<float>& Yref) {
    const int ol = out_len_of(j.n_cols);
    Yref.assign((size_t)j.n_rows * ol, 0.0f);
    for (int r = 0; r < j.n_rows; ++r)
        for (int c = 0; c < ol; ++c) {
            float acc = 0.0f;
            for (int t = 0; t < T; ++t)
                acc += u2f(gmem[j.h_off + t]) * u2f(gmem[j.x_off + r * j.n_cols + c + (T - 1) - t]);
            Yref[(size_t)r * ol + c] = acc;
        }
}

void push_cmd(hls::stream<streamutils::axi4s_word<32>>& s, const Job& j, FIROp op) {
    FIRCmd cmd;
    cmd.op = op;
    cmd.tx_id = j.tx_id;
    cmd.x_off = (ap_uint<32>)j.x_off;
    cmd.h_off = (ap_uint<32>)j.h_off;
    cmd.y_off = (ap_uint<32>)j.y_off;
    cmd.n_rows = (ap_uint<32>)j.n_rows;
    cmd.n_cols = (ap_uint<32>)j.n_cols;
    cmd.write_axi4_stream<32>(s, true);
}
void push_end(hls::stream<streamutils::axi4s_word<32>>& s) { Job z{}; push_cmd(s, z, FIROp::end); }

int check_Y(const std::vector<ap_uint<32>>& gmem, const Job& j, const char* tag) {
    std::vector<float> Yref; golden_into(gmem, j, Yref);
    const int ol = out_len_of(j.n_cols);
    int bad = 0;
    for (size_t i = 0; i < Yref.size(); ++i) {
        uint32_t a = (uint32_t)gmem[j.y_off + i], b = (uint32_t)f2u(Yref[i]);
        if (a != b) {
            if (bad < 6) std::fprintf(stderr, "  [%s tx=%u] Y mismatch at %zu: got 0x%08x exp 0x%08x\n",
                                      tag, j.tx_id, i, a, b);
            ++bad;
        }
    }
    if (bad == 0) std::printf("  [%s tx=%u %dx%d] Y bit-exact (%d elems)\n",
                              tag, j.tx_id, j.n_rows, j.n_cols, j.n_rows * ol);
    return bad;
}

std::vector<std::pair<unsigned, unsigned>> drain_resp(hls::stream<streamutils::axi4s_word<32>>& m_out) {
    std::vector<std::pair<unsigned, unsigned>> r;
    while (!m_out.empty()) {
        FIRResp resp;
        streamutils::tlast_status tl;
        resp.read_axi4_stream<32>(m_out, tl);
        r.emplace_back((unsigned)resp.tx_id, (unsigned)resp.status);
    }
    return r;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string scn = (argc > 1) ? argv[1] : "clean";
    std::vector<ap_uint<32>> gmem(GMEM_DEPTH, ap_uint<32>(0));
    hls::stream<streamutils::axi4s_word<32>> s_in("s_in");
    hls::stream<streamutils::axi4s_word<32>> m_out("m_out");
    int fails = 0;

    if (scn == "single" || scn == "two" || scn == "three") {
        const int n = (scn == "single") ? 1 : (scn == "two") ? 2 : 3;
        std::vector<Job> jobs;
        for (int i = 0; i < n; ++i) jobs.push_back(Job{4, 64, (unsigned)(100 + i), false, 0, 0, 0});
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, FIROp::fir);
        push_end(s_in);
        fir(s_in, m_out, gmem.data());
        for (auto& j : jobs) fails += check_Y(gmem, j, scn.c_str());
        auto resp = drain_resp(m_out);
        if ((int)resp.size() != n) { std::fprintf(stderr, "  expected %d resp, got %zu\n", n, resp.size()); ++fails; }
        for (auto& rp : resp) if (rp.second != 0) { std::fprintf(stderr, "  tx=%u status=%u\n", rp.first, rp.second); ++fails; }
        std::printf("  scenario '%s': %d job(s), %zu resp, all OK\n", scn.c_str(), n, resp.size());

    } else if (scn == "clean") {
        std::vector<Job> jobs = { Job{4,64,200,false,0,0,0}, Job{2,48,201,false,0,0,0}, Job{3,32,202,false,0,0,0} };
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, FIROp::fir);
        push_end(s_in);
        fir(s_in, m_out, gmem.data());
        for (auto& j : jobs) fails += check_Y(gmem, j, "clean");
        auto resp = drain_resp(m_out);
        if (resp.size() != jobs.size()) { std::fprintf(stderr, "  expected %zu resp, got %zu\n", jobs.size(), resp.size()); ++fails; }
        for (auto& rp : resp) if (rp.second != 0) { std::fprintf(stderr, "  tx=%u status=%u\n", rp.first, rp.second); ++fails; }

    } else if (scn == "error") {
        // batch 1: good, good, BAD-size, good (the bad job does NOT halt the pipeline).
        std::vector<Job> jobs = {
            Job{4,64,300,false,0,0,0}, Job{4,64,301,false,0,0,0},
            Job{4,4096,302,true,0,0,0}, Job{4,64,303,false,0,0,0},
        };
        layout_and_fill(jobs, gmem);
        for (auto& j : jobs) push_cmd(s_in, j, FIROp::fir);
        push_end(s_in);
        fir(s_in, m_out, gmem.data());
        fails += check_Y(gmem, jobs[0], "batch1");
        fails += check_Y(gmem, jobs[1], "batch1");
        fails += check_Y(gmem, jobs[3], "batch1");   // post-error good job still ran
        auto resp = drain_resp(m_out);
        const unsigned want_tx[4] = {300, 301, 302, 303};
        const unsigned want_st[4] = {0, 0, 1, 0};
        if (resp.size() != 4) { std::fprintf(stderr, "  batch1 expected 4 resp, got %zu\n", resp.size()); ++fails; }
        for (size_t i = 0; i < resp.size() && i < 4; ++i)
            if (resp[i].first != want_tx[i] || resp[i].second != want_st[i]) {
                std::fprintf(stderr, "  batch1 resp[%zu]=(tx=%u,st=%u) exp (tx=%u,st=%u)\n",
                             i, resp[i].first, resp[i].second, want_tx[i], want_st[i]); ++fails;
            }
        std::printf("  [batch1] %zu resp; bad tx=302 flagged, tx=303 still completed\n", resp.size());

        // batch 2 (RESTART): a second ap_start, one good job.
        Job j2{4, 64, 400, false, 0, 0, 0};
        std::vector<Job> b2 = {j2};
        layout_and_fill(b2, gmem);
        push_cmd(s_in, b2[0], FIROp::fir);
        push_end(s_in);
        fir(s_in, m_out, gmem.data());
        fails += check_Y(gmem, b2[0], "batch2");
        auto resp2 = drain_resp(m_out);
        if (resp2.size() != 1 || resp2[0].second != 0) { std::fprintf(stderr, "  batch2 restart resp wrong\n"); ++fails; }
        std::printf("  [batch2/restart] %zu resp, OK\n", resp2.size());

    } else {
        std::fprintf(stderr, "unknown scenario '%s'\n", scn.c_str());
        return 2;
    }

    if (fails == 0) {
        std::printf("WAVEFLOW_FIR_GEN_OK: scenario '%s' passed (bit-exact + per-job resp)\n", scn.c_str());
        return 0;
    }
    std::fprintf(stderr, "WAVEFLOW_FIR_GEN_FAIL: scenario '%s' had %d failure(s)\n", scn.c_str(), fails);
    return 1;
}
