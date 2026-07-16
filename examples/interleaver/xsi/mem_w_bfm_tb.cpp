// mem_w_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemWStream kernel (gen/mem_w_stream.cpp) in xsim on Windows.  Mirror of mem_r_bfm_tb.cpp.
//
// Gate: MemWStream drains N known words off s_in and pure-writes them to mem[w0 .. w0+N); the
// backing memory region equals the input bit-exact.  TB drives s_cmd (master), s_in (master), and
// the gmem0 AXI write slave (AWREADY / WREADY / B*); the read side + control are pinned to 0.
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

static const int  N        = 128;
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;
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

static uint64_t known_word(int i) { return ((uint64_t)i * 40503ULL + 7ULL); }

int main() {
    std::vector<uint64_t> mem(MEM_NW, 0);
    // Command carries an ELEMENT/WORD coordinate (= the old word-aligned byte addr / BPW). The
    // offset=slave register stays pinned to 0, so the kernel's m_mem[word_index] drives
    // AWADDR = word_index*BPW — the AXI slave's awaddr/BPW->word decode below is unchanged.
    const uint32_t word_index = (uint32_t)BASE_W;
    // MWCmd{addr, len, xfer_len, xfer_msg[8]} packs to 6 words at MEM_DW=64 (mirrors mem_r_bfm_tb.cpp).
    std::vector<uint64_t> cmd_words = {
        (uint64_t)word_index | ((uint64_t)(uint32_t)N << 32),
        0ULL,
        0ULL, 0ULL, 0ULL, 0ULL,
    };
    const int NCMDW = (int)cmd_words.size();

    std::string design = "xsim.dir/mem_w_stream/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "mem_w_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    // AXIS s_cmd (kernel slave; TB master)
    int P_cmd_data  = d.port("s_cmd_TDATA");
    int P_cmd_valid = d.port("s_cmd_TVALID");
    int P_cmd_ready = d.port("s_cmd_TREADY");
    // AXIS s_in (kernel slave; TB master)
    int P_in_data  = d.port("s_in_TDATA");
    int P_in_valid = d.port("s_in_TVALID");
    int P_in_ready = d.port("s_in_TREADY");
    // gmem0 write: kernel drives AW*/W*/BREADY, TB drives AWREADY/WREADY/B*
    int P_awvalid = d.port("m_axi_gmem0_AWVALID");
    int P_awready = d.port("m_axi_gmem0_AWREADY");
    int P_awaddr  = d.port("m_axi_gmem0_AWADDR");
    int P_awlen   = d.port("m_axi_gmem0_AWLEN");
    int P_wvalid  = d.port("m_axi_gmem0_WVALID");
    int P_wready  = d.port("m_axi_gmem0_WREADY");
    int P_wdata   = d.port("m_axi_gmem0_WDATA");
    int P_wstrb   = d.port("m_axi_gmem0_WSTRB");
    int P_wlast   = d.port("m_axi_gmem0_WLAST");
    int P_bvalid  = d.port("m_axi_gmem0_BVALID");
    int P_bready  = d.port("m_axi_gmem0_BREADY");

    const char* zero_ports[] = {
        "s_axi_control_AWVALID","s_axi_control_AWADDR","s_axi_control_WVALID","s_axi_control_WDATA",
        "s_axi_control_WSTRB","s_axi_control_ARVALID","s_axi_control_ARADDR","s_axi_control_RREADY",
        "s_axi_control_BREADY",
        "m_axi_gmem0_ARREADY","m_axi_gmem0_RVALID","m_axi_gmem0_RDATA","m_axi_gmem0_RLAST",
        "m_axi_gmem0_RRESP","m_axi_gmem0_RID","m_axi_gmem0_RUSER",
    };
    for (size_t i = 0; i < sizeof(zero_ports)/sizeof(zero_ports[0]); ++i) {
        int p = xsi.get_port_number(zero_ports[i]);
        if (p >= 0) d.putW(p, 0);
    }

    // Held TB-driven state + FSMs.
    uint32_t h_cmd_valid = 1;
    int      cmd_widx = 0;               // next s_cmd word to present
    int      in_idx = 0;                 // next data word to present
    uint32_t h_in_valid = 1;
    int      w_count = 0;                // words accepted by the write slave
    bool     saw_b = false;
    const uint32_t h_bready_unused = 0;  // (kernel drives BREADY; TB reads it)
    enum { AW_IDLE, W_RECV, B_RESP } g_state = AW_IDLE;
    uint64_t g_addrw = 0; uint32_t g_len = 0, g_wbeat = 0;
    uint32_t h_awready = 1, h_wready = 0, h_bvalid = 0;

    auto driveAll = [&]() {
        d.putW(P_cmd_data, (cmd_widx < NCMDW) ? cmd_words[cmd_widx] : 0);
        d.put1(P_cmd_valid, h_cmd_valid);
        d.putW(P_in_data, (in_idx < N) ? known_word(in_idx) : 0);
        d.put1(P_in_valid, h_in_valid);
        d.put1(P_awready, h_awready);
        d.put1(P_wready,  h_wready);
        d.put1(P_bvalid,  h_bvalid);
    };

    d.put1(P_rst_n, 0);
    driveAll();
    for (int k = 0; k < 16; ++k) { d.put1(P_clk, 0); xsi.run(10); d.put1(P_clk, 1); xsi.run(10); }
    d.put1(P_rst_n, 1);
    driveAll();

    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        if (w_count >= N && saw_b && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        d.put1(P_clk, 0); xsi.run(10);
        uint32_t s_cmd_ready = d.get1(P_cmd_ready);
        uint32_t s_in_ready  = d.get1(P_in_ready);
        uint32_t awvalid = d.get1(P_awvalid);
        uint64_t awaddr  = d.getW(P_awaddr);
        uint32_t awlen   = (uint32_t)(d.getW(P_awlen) & 0xFF);
        uint32_t wvalid  = d.get1(P_wvalid);
        uint64_t wdata   = d.getW(P_wdata);
        uint32_t wstrb   = (uint32_t)(d.getW(P_wstrb) & 0xFF);
        uint32_t wlast   = d.get1(P_wlast);
        uint32_t bready  = d.get1(P_bready);

        bool cmd_beat = (h_cmd_valid && s_cmd_ready);
        bool in_beat  = (h_in_valid && s_in_ready);
        bool aw_beat  = (g_state == AW_IDLE) && awvalid && h_awready;
        bool w_beat   = (g_state == W_RECV)  && wvalid && h_wready;
        bool b_beat   = (g_state == B_RESP)  && h_bvalid && bready;

        d.put1(P_clk, 1); xsi.run(10);

        if (cmd_beat && cmd_widx < NCMDW) {
            ++cmd_widx;
            h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u;
        }
        if (in_beat) { ++in_idx; h_in_valid = (in_idx < N) ? 1u : 0u; }

        if (aw_beat) {
            g_addrw = awaddr / BPW; g_len = awlen; g_wbeat = 0;
            g_state = W_RECV; h_awready = 0; h_wready = 1;
        } else if (w_beat) {
            uint64_t idx = g_addrw + g_wbeat;
            uint64_t cur = mem[idx];
            for (int b = 0; b < BPW; ++b) {
                if (wstrb & (1u << b)) {
                    uint64_t m = 0xFFull << (8 * b);
                    cur = (cur & ~m) | (wdata & m);
                }
            }
            mem[idx] = cur; ++w_count;
            if (wlast) { g_state = B_RESP; h_wready = 0; h_bvalid = 1; }
            else       { ++g_wbeat; }
        } else if (b_beat) {
            saw_b = true; g_state = AW_IDLE; h_bvalid = 0; h_awready = 1;
        }
        driveAll();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld w_count=%d/%d in_idx=%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, w_count, N, in_idx, cmd_widx, NCMDW, (int)g_state);
        xsi.close();
        return 1;
    }

    int fails = 0;
    for (int i = 0; i < N; ++i) {
        uint64_t exp = known_word(i), got = mem[BASE_W + i];
        if (got != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %d: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)got, (unsigned long long)exp);
            ++fails;
        }
    }
    std::printf("mem_w_stream XSI BFM: N=%d w_count=%d cycles=%ld\n", N, w_count, cyc);
    xsi.close();
    if (fails) { std::printf("FAILED test: %d mismatched words\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
