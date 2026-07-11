// interleaver_canon_bfm_tb.cpp — XSI BFM for the CANONICAL six-stage interleaver
// (gen/interleaver_canon.cpp): cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w,
// with one InterleaverCmd token per job forwarded through every stage (sob3's per-job pacing).  The
// definitive test that token forwarding breaks the done==#tasks+1 deadlock the mix (7/8) and P-SOB
// (6/8) variants hit.  Same boundary/memory model as the other interleaver BFMs; the ONLY difference
// is s_done carries the 2-word InterleaverCmd token per job (one job-done per 2 s_done beats).
//
// Adapted from sandbox/il_1d/xsi_task/il_bfm_tb.cpp.  Two differences from that byte-addressed sob3
// harness: (1) the command carries element/word OFFSETS (the word_index convention), and (2) the
// generated s_cmd is a 64-bit AXIS carrying the 128-bit InterleaverCmd as TWO beats.  Everything else
// (one flat memory behind gmem0 read + gmem1 write, the AXI-MM slave FSMs, per-job done-cycle
// recording) is identical.  The kernel's m_mem[word_index] drives ARADDR = word_index*BPW, so the
// slave's araddr/BPW -> word decode is unchanged.  ap_ctrl_none: driven directly through XSI (Vitis
// cosim is unreliable).  Per-job done cycles print the steady-state period (slope) = throughput.
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

// VERDICT: PASS — forwarding a per-job token through every stage BREAKS the done==#tasks+1 law.
// nj=8 -> 8/8 (mix hung at 7/8, P-SOB at 6/8) and nj=16 -> 16/16 (truly unbounded, not a higher
// tasks+1), bit-exact Y[i]=X[P[i]] with commit-timed done tokens. Steady-state 414 cyc/job (above the
// load-bound ~296: the strict per-job token pacing trades some overlap for the robustness).
static const int  N        = 256;
static const int  NJ       = 8;    // >4 so the steady-state period (slope) separates from fill latency
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  LW       = MEM_DW / 32;            // 32-bit elems per word = 2
static const int  NW       = (N + LW - 1) / LW;      // words per array = 128
static const int  MEM_NW   = 8192;
static const long MAX_CYCLES = 2000000L;

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static int   Pidx(int i)        { return ((i * 13 + 5) % N); }
static float Xval(int j, int k) { return (float)((float)k * 0.5f - 3.0f + (float)j); }

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

int main() {
    // 1) Shared memory + commands. Layout per job: P then X then Y, NW words each, base = j*3*NW.
    std::vector<uint64_t> mem(MEM_NW, 0);
    int yw[NJ];
    std::vector<uint64_t> cmd_flat;                 // 2 words per InterleaverCmd (word offsets)
    for (int j = 0; j < NJ; ++j) {
        int base = j * 3 * NW;
        int pw = base, xw = base + NW, yj = base + 2 * NW;
        yw[j] = yj;
        for (int i = 0; i < N; ++i) {
            uint32_t pbits = (uint32_t)Pidx(i);
            uint32_t xbits = fbits(Xval(j, i));
            int      lane  = i % LW;
            uint64_t shift = (uint64_t)(lane * 32);
            uint64_t keep  = ~((uint64_t)0xFFFFFFFFull << shift);
            mem[pw + i/LW] = (mem[pw + i/LW] & keep) | ((uint64_t)pbits << shift);
            mem[xw + i/LW] = (mem[xw + i/LW] & keep) | ((uint64_t)xbits << shift);
        }
        // InterleaverCmd{p_off,x_off,y_off,n} -> word0 = p_off|(x_off<<32), word1 = y_off|(n<<32).
        cmd_flat.push_back((uint64_t)(uint32_t)pw | ((uint64_t)(uint32_t)xw << 32));
        cmd_flat.push_back((uint64_t)(uint32_t)yj | ((uint64_t)(uint32_t)N << 32));
    }
    const int NCMDW = (int)cmd_flat.size();

    // 2) Open the elaborated design.
    std::string design = "xsim.dir/interleaver_canon/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "interleaver_canon_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    int P_cmd_data  = d.port("s_cmd_TDATA");
    int P_cmd_valid = d.port("s_cmd_TVALID");
    int P_cmd_ready = d.port("s_cmd_TREADY");
    int P_done_valid = d.port("s_done_TVALID");
    int P_done_ready = d.port("s_done_TREADY");
    // gmem0 read: kernel drives AR*/RREADY, TB drives ARREADY/R*
    int P_g0_arvalid = d.port("m_axi_gmem0_ARVALID");
    int P_g0_arready = d.port("m_axi_gmem0_ARREADY");
    int P_g0_araddr  = d.port("m_axi_gmem0_ARADDR");
    int P_g0_arlen   = d.port("m_axi_gmem0_ARLEN");
    int P_g0_rvalid  = d.port("m_axi_gmem0_RVALID");
    int P_g0_rready  = d.port("m_axi_gmem0_RREADY");
    int P_g0_rdata   = d.port("m_axi_gmem0_RDATA");
    int P_g0_rlast   = d.port("m_axi_gmem0_RLAST");
    // gmem1 write: kernel drives AW*/W*/BREADY, TB drives AWREADY/WREADY/B*
    int P_g1_awvalid = d.port("m_axi_gmem1_AWVALID");
    int P_g1_awready = d.port("m_axi_gmem1_AWREADY");
    int P_g1_awaddr  = d.port("m_axi_gmem1_AWADDR");
    int P_g1_awlen   = d.port("m_axi_gmem1_AWLEN");
    int P_g1_wvalid  = d.port("m_axi_gmem1_WVALID");
    int P_g1_wready  = d.port("m_axi_gmem1_WREADY");
    int P_g1_wdata   = d.port("m_axi_gmem1_WDATA");
    int P_g1_wstrb   = d.port("m_axi_gmem1_WSTRB");
    int P_g1_wlast   = d.port("m_axi_gmem1_WLAST");
    int P_g1_bvalid  = d.port("m_axi_gmem1_BVALID");
    int P_g1_bready  = d.port("m_axi_gmem1_BREADY");

    const char* zero_ports[] = {
        "s_axi_control_AWVALID","s_axi_control_AWADDR","s_axi_control_WVALID","s_axi_control_WDATA",
        "s_axi_control_WSTRB","s_axi_control_ARVALID","s_axi_control_ARADDR","s_axi_control_RREADY",
        "s_axi_control_BREADY",
        "m_axi_gmem0_AWREADY","m_axi_gmem0_WREADY","m_axi_gmem0_BVALID","m_axi_gmem0_BRESP",
        "m_axi_gmem0_BID","m_axi_gmem0_BUSER","m_axi_gmem0_RRESP","m_axi_gmem0_RID","m_axi_gmem0_RUSER",
        "m_axi_gmem1_ARREADY","m_axi_gmem1_RVALID","m_axi_gmem1_RDATA","m_axi_gmem1_RLAST",
        "m_axi_gmem1_RRESP","m_axi_gmem1_RID","m_axi_gmem1_RUSER","m_axi_gmem1_BRESP",
        "m_axi_gmem1_BID","m_axi_gmem1_BUSER",
    };
    for (size_t i = 0; i < sizeof(zero_ports)/sizeof(zero_ports[0]); ++i) {
        int p = xsi.get_port_number(zero_ports[i]);
        if (p >= 0) d.putW(p, 0);
    }

    // 3) Held TB-driven state + FSMs.
    int      cmd_widx = 0;
    uint32_t h_cmd_valid = 0;
    int      done_count = 0;
    int      done_beats = 0;               // s_done carries the 2-word InterleaverCmd token per job
    long     done_cyc[NJ];
    const uint32_t h_done_ready = 1;
    enum { AR_IDLE, R_SEND } g0_state = AR_IDLE;
    uint64_t g0_addrw = 0; uint32_t g0_len = 0, g0_beat = 0;
    uint32_t h_g0_arready = 1, h_g0_rvalid = 0, h_g0_rlast = 0; uint64_t h_g0_rdata = 0;
    enum { AW_IDLE, W_RECV, B_RESP } g1_state = AW_IDLE;
    uint64_t g1_addrw = 0; uint32_t g1_len = 0, g1_wbeat = 0;
    uint32_t h_g1_awready = 1, h_g1_wready = 0, h_g1_bvalid = 0;

    auto driveAll = [&]() {
        d.putW(P_cmd_data, (cmd_widx < NCMDW) ? cmd_flat[cmd_widx] : 0);
        d.put1(P_cmd_valid, h_cmd_valid);
        d.put1(P_done_ready, h_done_ready);
        d.put1(P_g0_arready, h_g0_arready);
        d.put1(P_g0_rvalid,  h_g0_rvalid);
        d.putW(P_g0_rdata,   h_g0_rdata);
        d.put1(P_g0_rlast,   h_g0_rlast);
        d.put1(P_g1_awready, h_g1_awready);
        d.put1(P_g1_wready,  h_g1_wready);
        d.put1(P_g1_bvalid,  h_g1_bvalid);
    };

    // 4) Reset.
    d.put1(P_rst_n, 0);
    h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u;
    driveAll();
    for (int k = 0; k < 16; ++k) { d.put1(P_clk, 0); xsi.run(10); d.put1(P_clk, 1); xsi.run(10); }
    d.put1(P_rst_n, 1);
    driveAll();

    // 5) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NJ && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        d.put1(P_clk, 0); xsi.run(10);
        uint32_t s_cmd_ready = d.get1(P_cmd_ready);
        uint32_t s_done_val  = d.get1(P_done_valid);
        uint32_t g0_arvalid  = d.get1(P_g0_arvalid);
        uint64_t g0_araddr   = d.getW(P_g0_araddr);
        uint32_t g0_arlen    = (uint32_t)(d.getW(P_g0_arlen) & 0xFF);
        uint32_t g0_rready   = d.get1(P_g0_rready);
        uint32_t g1_awvalid  = d.get1(P_g1_awvalid);
        uint64_t g1_awaddr   = d.getW(P_g1_awaddr);
        uint32_t g1_awlen    = (uint32_t)(d.getW(P_g1_awlen) & 0xFF);
        uint32_t g1_wvalid   = d.get1(P_g1_wvalid);
        uint64_t g1_wdata    = d.getW(P_g1_wdata);
        uint32_t g1_wstrb    = (uint32_t)(d.getW(P_g1_wstrb) & 0xFF);
        uint32_t g1_wlast    = d.get1(P_g1_wlast);
        uint32_t g1_bready   = d.get1(P_g1_bready);

        bool cmd_beat   = (h_cmd_valid && s_cmd_ready);
        bool done_beat  = (s_done_val && h_done_ready);
        bool g0_ar_beat = (g0_state == AR_IDLE) && g0_arvalid && h_g0_arready;
        bool g0_r_beat  = (g0_state == R_SEND)  && h_g0_rvalid && g0_rready;
        bool g1_aw_beat = (g1_state == AW_IDLE) && g1_awvalid && h_g1_awready;
        bool g1_w_beat  = (g1_state == W_RECV)  && g1_wvalid && h_g1_wready;
        bool g1_b_beat  = (g1_state == B_RESP)  && h_g1_bvalid && g1_bready;

        d.put1(P_clk, 1); xsi.run(10);

        if (cmd_beat && cmd_widx < NCMDW) { ++cmd_widx; h_cmd_valid = (cmd_widx < NCMDW) ? 1u : 0u; }
        // s_done carries the 2-word InterleaverCmd token per job -> one job done per 2 beats.
        if (done_beat && (++done_beats % 2 == 0)) { done_cyc[done_count] = cyc; ++done_count; }

        if (g0_ar_beat) {
            g0_addrw = g0_araddr / BPW; g0_len = g0_arlen; g0_beat = 0;
            g0_state = R_SEND; h_g0_arready = 0; h_g0_rvalid = 1;
            h_g0_rdata = mem[g0_addrw]; h_g0_rlast = (g0_len == 0) ? 1u : 0u;
        } else if (g0_r_beat) {
            if (g0_beat >= g0_len) { g0_state = AR_IDLE; h_g0_rvalid = 0; h_g0_rlast = 0; h_g0_arready = 1; }
            else { ++g0_beat; h_g0_rdata = mem[g0_addrw + g0_beat]; h_g0_rlast = (g0_beat >= g0_len) ? 1u : 0u; }
        }

        if (g1_aw_beat) {
            g1_addrw = g1_awaddr / BPW; g1_len = g1_awlen; g1_wbeat = 0;
            g1_state = W_RECV; h_g1_awready = 0; h_g1_wready = 1;
        } else if (g1_w_beat) {
            uint64_t idx = g1_addrw + g1_wbeat;
            uint64_t cur = mem[idx];
            for (int b = 0; b < BPW; ++b) {
                if (g1_wstrb & (1u << b)) {
                    uint64_t m = 0xFFull << (8 * b);
                    cur = (cur & ~m) | (g1_wdata & m);
                }
            }
            mem[idx] = cur;
            if (g1_wlast) { g1_state = B_RESP; h_g1_wready = 0; h_g1_bvalid = 1; }
            else          { ++g1_wbeat; }
        } else if (g1_b_beat) {
            g1_state = AW_IDLE; h_g1_bvalid = 0; h_g1_awready = 1;
        }

        driveAll();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT after %ld cycles, done=%d/%d (cmd_widx=%d/%d "
                     "g0=%d g1=%d)\n  done cycles:", cyc, done_count, NJ, cmd_widx, NCMDW,
                     (int)g0_state, (int)g1_state);
        for (int j = 0; j < done_count; ++j)
            std::fprintf(stderr, " j%d=%ld(%s%ld)", j, done_cyc[j],
                         j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
        std::fprintf(stderr, "\n");
        xsi.close();
        return 1;
    }

    // 6) Golden check: every Y[i] == X[P[i]] bit-exact, unpacked from mem.
    int fails = 0;
    for (int j = 0; j < NJ; ++j) {
        for (int i = 0; i < N; ++i) {
            uint64_t w = mem[yw[j] + i/LW];
            uint32_t got = (uint32_t)((w >> ((i % LW) * 32)) & 0xFFFFFFFFull);
            uint32_t exp = fbits(Xval(j, Pidx(i)));
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d elem %d: got 0x%08x exp 0x%08x\n",
                                            j, i, got, exp);
                ++fails;
            }
        }
    }

    long latency = (drain >= 0) ? drain : cyc;
    std::printf("interleaver_canon XSI BFM: n=%d nj=%d cycles=%ld done=%d/%d\n", N, NJ, latency, done_count, NJ);
    std::printf("  per-job done cycles (period in parens):\n   ");
    for (int j = 0; j < done_count; ++j)
        std::printf(" j%d=%ld(%s%ld)", j, done_cyc[j],
                    j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
    std::printf("\n  steady-state period ~= n/job (2*(n/LW) read words) for MEM_DW=64; sob3 ~= 295\n");
    xsi.close();
    if (fails) { std::printf("FAILED test: %d mismatched elements\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
