// mem_copy_bfm_tb.cpp — XSI cycle-based BFM that RTL-verifies the generated free-running MemCopy
// composite (gen/mem_copy.cpp) in xsim on Windows.  The Phase-2 (Gate 2) harness: it proves the
// GENERATED multi-hls::task top (Sequencer -> MemRStream -> MemWStream, wired by internal FIFOs)
// memcpy's a word run bit-exact through real RTL.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI with hand-written behavioral models of its four boundary interfaces:
//   * s_cmd  (AXIS slave  on the kernel; TB master) — streams CopyCmd{src_off,dst_off,n_words}, 2
//             words each; two commands back-to-back exercise the hls::task re-fire across jobs.
//   * s_done (AXIS master on the kernel; TB slave)  — one completion token per job.
//   * gmem0  (AXI-MM read)  — kernel drives AR*/RREADY; TB serves reads from the shared memory.
//   * gmem1  (AXI-MM write) — kernel drives AW*/W*/BREADY; TB writes into the SAME shared memory.
// The two m_axi bundles share one flat `mem` (offset=slave registers pinned to 0), so the copy reads
// the source region and writes the destination region of one buffer — out region == in region.
//
// Cycle protocol (per il_bfm): sample kernel OUTPUTS while clk is LOW, detect beats (VALID && READY),
// drive clk high, apply beat effects + advance every channel FSM.
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  MEM_NW   = 8192;
static const int  N        = 128;                    // words per copy
static const int  NUM_CMDS = 2;                      // back-to-back jobs
static const int  SRC_W[NUM_CMDS] = { 64, 512 };     // source word offsets
static const int  DST_W[NUM_CMDS] = { 2048, 4096 };  // destination word offsets
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

static uint64_t known_word(int job, int i) {
    return ((uint64_t)i * 2654435761ULL + 12345ULL + (uint64_t)job * 7919ULL);
}

int main() {
    // 1) Shared backing memory: known patterns in each source region.
    std::vector<uint64_t> mem(MEM_NW, 0);
    for (int j = 0; j < NUM_CMDS; ++j)
        for (int i = 0; i < N; ++i) mem[SRC_W[j] + i] = known_word(j, i);

    // CopyCmd stream words (2 per command): word0 = src|(dst<<32), word1 = n_words.
    std::vector<uint64_t> cmd_words;
    for (int j = 0; j < NUM_CMDS; ++j) {
        cmd_words.push_back((uint64_t)(uint32_t)SRC_W[j] | ((uint64_t)(uint32_t)DST_W[j] << 32));
        cmd_words.push_back((uint64_t)(uint32_t)N);
    }
    const int NCMDW = (int)cmd_words.size();

    // 2) Open the elaborated design.
    std::string design = "xsim.dir/mem_copy/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "mem_copy_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    // AXIS s_cmd (kernel slave; TB master)
    int P_cmd_data  = d.port("s_cmd_TDATA");
    int P_cmd_valid = d.port("s_cmd_TVALID");
    int P_cmd_ready = d.port("s_cmd_TREADY");
    // AXIS s_done (kernel master; TB slave)
    int P_done_data  = d.port("s_done_TDATA");
    int P_done_valid = d.port("s_done_TVALID");
    int P_done_ready = d.port("s_done_TREADY");
    // gmem0 read: kernel drives AR*/RREADY, TB drives ARREADY/R*
    int P_arvalid = d.port("m_axi_gmem0_ARVALID");
    int P_arready = d.port("m_axi_gmem0_ARREADY");
    int P_araddr  = d.port("m_axi_gmem0_ARADDR");
    int P_arlen   = d.port("m_axi_gmem0_ARLEN");
    int P_rvalid  = d.port("m_axi_gmem0_RVALID");
    int P_rready  = d.port("m_axi_gmem0_RREADY");
    int P_rdata   = d.port("m_axi_gmem0_RDATA");
    int P_rlast   = d.port("m_axi_gmem0_RLAST");
    // gmem1 write: kernel drives AW*/W*/BREADY, TB drives AWREADY/WREADY/B*
    int P_awvalid = d.port("m_axi_gmem1_AWVALID");
    int P_awready = d.port("m_axi_gmem1_AWREADY");
    int P_awaddr  = d.port("m_axi_gmem1_AWADDR");
    int P_awlen   = d.port("m_axi_gmem1_AWLEN");
    int P_wvalid  = d.port("m_axi_gmem1_WVALID");
    int P_wready  = d.port("m_axi_gmem1_WREADY");
    int P_wdata   = d.port("m_axi_gmem1_WDATA");
    int P_wstrb   = d.port("m_axi_gmem1_WSTRB");
    int P_wlast   = d.port("m_axi_gmem1_WLAST");
    int P_bvalid  = d.port("m_axi_gmem1_BVALID");
    int P_bready  = d.port("m_axi_gmem1_BREADY");

    // Pin every other TB-driven input to 0 (offset=slave registers -> base 0; the unused write side
    // of gmem0 and the unused read side of gmem1; quiescent control).
    const char* zero_ports[] = {
        "s_axi_control_AWVALID","s_axi_control_AWADDR","s_axi_control_WVALID","s_axi_control_WDATA",
        "s_axi_control_WSTRB","s_axi_control_ARVALID","s_axi_control_ARADDR","s_axi_control_RREADY",
        "s_axi_control_BREADY",
        // gmem0 unused write channels
        "m_axi_gmem0_AWREADY","m_axi_gmem0_WREADY","m_axi_gmem0_BVALID","m_axi_gmem0_BRESP",
        "m_axi_gmem0_BID","m_axi_gmem0_BUSER","m_axi_gmem0_RRESP","m_axi_gmem0_RID","m_axi_gmem0_RUSER",
        // gmem1 unused read channels
        "m_axi_gmem1_ARREADY","m_axi_gmem1_RVALID","m_axi_gmem1_RDATA","m_axi_gmem1_RLAST",
        "m_axi_gmem1_RRESP","m_axi_gmem1_RID","m_axi_gmem1_RUSER","m_axi_gmem1_BID","m_axi_gmem1_BUSER",
    };
    for (size_t i = 0; i < sizeof(zero_ports)/sizeof(zero_ports[0]); ++i) {
        int p = xsi.get_port_number(zero_ports[i]);
        if (p >= 0) d.putW(p, 0);
    }

    // 3) Held TB-driven state + FSMs.
    int      cmd_widx = 0;               // next s_cmd word to present
    uint32_t h_cmd_valid = 1;
    int      done_count = 0;
    const uint32_t h_done_ready = 1;     // always ready to accept done tokens
    // gmem0 read FSM
    enum { AR_IDLE, R_SEND } g_rstate = AR_IDLE;
    uint64_t g_raddrw = 0; uint32_t g_rlen = 0, g_rbeat = 0;
    uint32_t h_arready = 1, h_rvalid = 0, h_rlast = 0; uint64_t h_rdata = 0;
    // gmem1 write FSM
    enum { AW_IDLE, W_RECV, B_RESP } g_wstate = AW_IDLE;
    uint64_t g_waddrw = 0; uint32_t g_wlen = 0, g_wbeat = 0;
    int      w_count = 0;
    uint32_t h_awready = 1, h_wready = 0, h_bvalid = 0;

    auto driveAll = [&]() {
        d.putW(P_cmd_data, (cmd_widx < NCMDW) ? cmd_words[cmd_widx] : 0);
        d.put1(P_cmd_valid, h_cmd_valid);
        d.put1(P_done_ready, h_done_ready);
        d.put1(P_arready, h_arready);
        d.put1(P_rvalid,  h_rvalid);
        d.putW(P_rdata,   h_rdata);
        d.put1(P_rlast,   h_rlast);
        d.put1(P_awready, h_awready);
        d.put1(P_wready,  h_wready);
        d.put1(P_bvalid,  h_bvalid);
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
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NUM_CMDS && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        d.put1(P_clk, 0); xsi.run(10);      // low phase: sample outputs
        uint32_t s_cmd_ready  = d.get1(P_cmd_ready);
        uint32_t s_done_valid = d.get1(P_done_valid);
        uint32_t arvalid = d.get1(P_arvalid);
        uint64_t araddr  = d.getW(P_araddr);
        uint32_t arlen   = (uint32_t)(d.getW(P_arlen) & 0xFF);
        uint32_t rready  = d.get1(P_rready);
        uint32_t awvalid = d.get1(P_awvalid);
        uint64_t awaddr  = d.getW(P_awaddr);
        uint32_t awlen   = (uint32_t)(d.getW(P_awlen) & 0xFF);
        uint32_t wvalid  = d.get1(P_wvalid);
        uint64_t wdata   = d.getW(P_wdata);
        uint32_t wstrb   = (uint32_t)(d.getW(P_wstrb) & 0xFF);
        uint32_t wlast   = d.get1(P_wlast);
        uint32_t bready  = d.get1(P_bready);

        bool cmd_beat  = (h_cmd_valid && s_cmd_ready);
        bool done_beat = (s_done_valid && h_done_ready);
        bool ar_beat   = (g_rstate == AR_IDLE) && arvalid && h_arready;
        bool r_beat    = (g_rstate == R_SEND)  && h_rvalid && rready;
        bool aw_beat   = (g_wstate == AW_IDLE) && awvalid && h_awready;
        bool w_beat    = (g_wstate == W_RECV)  && wvalid && h_wready;
        bool b_beat    = (g_wstate == B_RESP)  && h_bvalid && bready;

        d.put1(P_clk, 1); xsi.run(10);      // rising edge

        // AXIS s_cmd master: advance through the command words.
        if (cmd_beat && cmd_widx < NCMDW) {
            ++cmd_widx;
            h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u;
        }
        // AXIS s_done slave: count completion tokens.
        if (done_beat) ++done_count;

        // gmem0 read FSM.
        if (ar_beat) {
            g_raddrw = araddr / BPW; g_rlen = arlen; g_rbeat = 0;
            g_rstate = R_SEND; h_arready = 0; h_rvalid = 1;
            h_rdata = mem[g_raddrw]; h_rlast = (g_rlen == 0) ? 1u : 0u;
        } else if (r_beat) {
            if (g_rbeat >= g_rlen) { g_rstate = AR_IDLE; h_rvalid = 0; h_rlast = 0; h_arready = 1; }
            else { ++g_rbeat; h_rdata = mem[g_raddrw + g_rbeat]; h_rlast = (g_rbeat >= g_rlen) ? 1u : 0u; }
        }

        // gmem1 write FSM.
        if (aw_beat) {
            g_waddrw = awaddr / BPW; g_wlen = awlen; g_wbeat = 0;
            g_wstate = W_RECV; h_awready = 0; h_wready = 1;
        } else if (w_beat) {
            uint64_t idx = g_waddrw + g_wbeat;
            uint64_t cur = mem[idx];
            for (int b = 0; b < BPW; ++b) {
                if (wstrb & (1u << b)) {
                    uint64_t m = 0xFFull << (8 * b);
                    cur = (cur & ~m) | (wdata & m);
                }
            }
            mem[idx] = cur; ++w_count;
            if (wlast) { g_wstate = B_RESP; h_wready = 0; h_bvalid = 1; }
            else       { ++g_wbeat; }
        } else if (b_beat) {
            g_wstate = AW_IDLE; h_bvalid = 0; h_awready = 1;
        }
        driveAll();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld done=%d/%d w_count=%d cmd_widx=%d/%d "
                     "g_rstate=%d g_wstate=%d\n",
                     cyc, done_count, NUM_CMDS, w_count, cmd_widx, NCMDW,
                     (int)g_rstate, (int)g_wstate);
        xsi.close();
        return 1;
    }

    // 6) Golden check: every destination region equals its source region (a memcpy).
    int fails = 0;
    for (int j = 0; j < NUM_CMDS; ++j) {
        for (int i = 0; i < N; ++i) {
            uint64_t exp = known_word(j, i), got = mem[DST_W[j] + i];
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d word %d: got 0x%016llx exp 0x%016llx\n",
                                            j, i, (unsigned long long)got, (unsigned long long)exp);
                ++fails;
            }
        }
    }
    std::printf("mem_copy XSI BFM: jobs=%d N=%d done=%d w_count=%d cycles=%ld\n",
                NUM_CMDS, N, done_count, w_count, cyc);
    xsi.close();
    if (fails || done_count != NUM_CMDS) {
        std::printf("FAILED test: %d mismatches, done=%d/%d\n", fails, done_count, NUM_CMDS);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
