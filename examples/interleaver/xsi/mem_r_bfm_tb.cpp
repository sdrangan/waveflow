// mem_r_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemRStream kernel (gen/mem_r_stream.cpp) in xsim on Windows.  Adapted from the interleaver
// sandbox il_bfm_tb.cpp (the reusable ap_ctrl_none-tile harness).
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the
// elaborated RTL directly through XSI with a hand-written AXI-MM read-slave + AXIS master (s_cmd)
// / slave (m_out) behavioral model.  Gate: MemRStream bursts mem[w0 .. w0+N) onto m_out, and the
// collected stream equals the memory region bit-exact.
//
// Cycle protocol (per il_bfm): sample kernel OUTPUTS while clk is LOW (registered outputs are
// stable and present the value the upcoming rising edge consummates); detect AXI/AXIS beats
// (VALID && READY) off those samples + the held TB inputs; drive clk high; apply beat effects and
// advance every channel FSM.  Sampling in the low phase is essential (an AXIS master deasserts
// TVALID the cycle after a beat).
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

static const int  N        = 128;                    // words to burst
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;                     // region base (word index)
static const long MAX_CYCLES = 2000000L;

typedef s_xsi_vlog_logicval LV;

struct Dut {
    Xsi::Loader& x;
    explicit Dut(Xsi::Loader& xx) : x(xx) {}
    int port(const char* name) {
        int p = x.get_port_number(name);
        if (p < 0) { std::fprintf(stderr, "FATAL: port '%s' not found\n", name); std::exit(3); }
        return p;
    }
    void put1(int p, uint32_t b)  { LV v; v.aVal = b & 1u; v.bVal = 0; x.put_value(p, &v); }
    void putW(int p, uint64_t val){ LV v[4]; for (int k=0;k<4;k++){ v[k].aVal=(uint32_t)(val>>(32*k)); v[k].bVal=0; } x.put_value(p, v); }
    uint32_t get1(int p)          { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return v[0].aVal & 1u; }
    uint64_t getW(int p)          { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return ((uint64_t)v[1].aVal<<32) | v[0].aVal; }
};

static uint64_t known_word(int i) { return ((uint64_t)i * 2654435761ULL + 12345ULL); }

int main() {
    // 1) Backing memory: known pattern in [BASE_W, BASE_W+N).
    std::vector<uint64_t> mem(MEM_NW, 0);
    for (int i = 0; i < N; ++i) mem[BASE_W + i] = known_word(i);
    // MRCmd{addr, len, xfer_len, xfer_msg[8]} packs to 6 words at MEM_DW=64: word0 = addr|(len<<32),
    // word1 = xfer_len|(xfer_msg[0]<<32), words2-5 = xfer_msg[1:8] (2 per word) — all zero here (the
    // job-index cookie is only exercised by the mem_copy composite BFM).
    const uint32_t word_index = (uint32_t)BASE_W;
    std::vector<uint64_t> cmd_words = {
        (uint64_t)word_index | ((uint64_t)(uint32_t)N << 32),
        0ULL,
        0ULL, 0ULL, 0ULL, 0ULL,
    };
    const int NCMDW = (int)cmd_words.size();

    // 2) Open the elaborated design.
    std::string design = "xsim.dir/mem_r_stream/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "mem_r_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    // AXIS s_cmd (kernel slave; TB master)
    int P_cmd_data  = d.port("s_cmd_TDATA");
    int P_cmd_valid = d.port("s_cmd_TVALID");
    int P_cmd_ready = d.port("s_cmd_TREADY");
    // AXIS m_out (kernel master; TB slave)
    int P_out_data  = d.port("m_out_TDATA");
    int P_out_valid = d.port("m_out_TVALID");
    int P_out_ready = d.port("m_out_TREADY");
    // gmem0 read: kernel drives AR*/RREADY, TB drives ARREADY/R*
    int P_arvalid = d.port("m_axi_gmem0_ARVALID");
    int P_arready = d.port("m_axi_gmem0_ARREADY");
    int P_araddr  = d.port("m_axi_gmem0_ARADDR");
    int P_arlen   = d.port("m_axi_gmem0_ARLEN");
    int P_rvalid  = d.port("m_axi_gmem0_RVALID");
    int P_rready  = d.port("m_axi_gmem0_RREADY");
    int P_rdata   = d.port("m_axi_gmem0_RDATA");
    int P_rlast   = d.port("m_axi_gmem0_RLAST");

    // Pin every other TB-driven input to 0 (offset base 0, unused write side, quiescent control).
    const char* zero_ports[] = {
        "s_axi_control_AWVALID","s_axi_control_AWADDR","s_axi_control_WVALID","s_axi_control_WDATA",
        "s_axi_control_WSTRB","s_axi_control_ARVALID","s_axi_control_ARADDR","s_axi_control_RREADY",
        "s_axi_control_BREADY",
        "m_axi_gmem0_AWREADY","m_axi_gmem0_WREADY","m_axi_gmem0_BVALID","m_axi_gmem0_BRESP",
        "m_axi_gmem0_BID","m_axi_gmem0_BUSER","m_axi_gmem0_RRESP","m_axi_gmem0_RID","m_axi_gmem0_RUSER",
    };
    for (size_t i = 0; i < sizeof(zero_ports)/sizeof(zero_ports[0]); ++i) {
        int p = xsi.get_port_number(zero_ports[i]);
        if (p >= 0) d.putW(p, 0);
    }

    // 3) Held TB-driven state + FSMs.
    int      cmd_widx = 0;               // next s_cmd word to present
    uint32_t h_cmd_valid = 1;
    std::vector<uint64_t> got; got.reserve(N);
    const uint32_t h_out_ready = 1;      // always ready to receive
    enum { AR_IDLE, R_SEND } g_state = AR_IDLE;
    uint64_t g_addrw = 0; uint32_t g_len = 0, g_beat = 0;
    uint32_t h_arready = 1, h_rvalid = 0, h_rlast = 0; uint64_t h_rdata = 0;

    auto driveAll = [&]() {
        d.putW(P_cmd_data, (cmd_widx < NCMDW) ? cmd_words[cmd_widx] : 0);
        d.put1(P_cmd_valid, h_cmd_valid);
        d.put1(P_out_ready, h_out_ready);
        d.put1(P_arready, h_arready);
        d.put1(P_rvalid,  h_rvalid);
        d.putW(P_rdata,   h_rdata);
        d.put1(P_rlast,   h_rlast);
    };

    // 4) Reset.
    d.put1(P_rst_n, 0);
    driveAll();
    for (int k = 0; k < 16; ++k) { d.put1(P_clk, 0); xsi.run(10); d.put1(P_clk, 1); xsi.run(10); }
    d.put1(P_rst_n, 1);
    driveAll();

    // 5) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        if ((int)got.size() >= N && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        d.put1(P_clk, 0); xsi.run(10);      // low phase: sample outputs
        uint32_t s_cmd_ready = d.get1(P_cmd_ready);
        uint32_t s_out_valid = d.get1(P_out_valid);
        uint64_t s_out_data  = d.getW(P_out_data);
        uint32_t arvalid = d.get1(P_arvalid);
        uint64_t araddr  = d.getW(P_araddr);
        uint32_t arlen   = (uint32_t)(d.getW(P_arlen) & 0xFF);
        uint32_t rready  = d.get1(P_rready);

        bool cmd_beat = (h_cmd_valid && s_cmd_ready);
        bool out_beat = (s_out_valid && h_out_ready);
        bool ar_beat  = (g_state == AR_IDLE) && arvalid && h_arready;
        bool r_beat   = (g_state == R_SEND)  && h_rvalid && rready;

        d.put1(P_clk, 1); xsi.run(10);      // rising edge

        if (cmd_beat && cmd_widx < NCMDW) {
            ++cmd_widx;
            h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u;
        }
        if (out_beat) got.push_back(s_out_data);

        if (ar_beat) {
            g_addrw = araddr / BPW; g_len = arlen; g_beat = 0;
            g_state = R_SEND; h_arready = 0; h_rvalid = 1;
            h_rdata = mem[g_addrw]; h_rlast = (g_len == 0) ? 1u : 0u;
        } else if (r_beat) {
            if (g_beat >= g_len) { g_state = AR_IDLE; h_rvalid = 0; h_rlast = 0; h_arready = 1; }
            else { ++g_beat; h_rdata = mem[g_addrw + g_beat]; h_rlast = (g_beat >= g_len) ? 1u : 0u; }
        }
        driveAll();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld got=%zu/%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, got.size(), N, cmd_widx, NCMDW, (int)g_state);
        xsi.close();
        return 1;
    }

    // 6) Golden check.
    int fails = 0;
    for (int i = 0; i < N; ++i) {
        uint64_t exp = known_word(i);
        if (i >= (int)got.size() || got[i] != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %d: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)(i < (int)got.size() ? got[i] : 0),
                                        (unsigned long long)exp);
            ++fails;
        }
    }
    std::printf("mem_r_stream XSI BFM: N=%d collected=%zu cycles=%ld\n", N, got.size(), cyc);
    xsi.close();
    if (fails || (int)got.size() != N) { std::printf("FAILED test: %d mismatches (got %zu)\n", fails, got.size()); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
