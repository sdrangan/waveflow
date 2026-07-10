// il_bfm_tb.cpp — XSI cycle-based bus-functional-model testbench that RTL-verifies the
// free-running hls::task interleaver (interleaver_task.cpp) in xsim on Windows.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it (COSIM 212-345), so we
// drive the elaborated RTL directly through XSI (xsi_loader: get_port_number / put_value / get_value
// / run) with a hand-written AXI-MM slave + AXIS master/slave behavioral model. This is the reusable
// AXI-MM-slave harness for every free-running (SALSA-tile) kernel.
//
// Memory model: one flat std::vector<uint64_t> mem(8192) (MEM_DW=64, byte/8 = word), shared behind
// gmem0 (kernel reads P/X) and gmem1 (kernel writes Y) — mirroring interleaver_task_tb.cpp's single
// physical memory behind both ports. Init layout is byte-identical to that golden TB (per job:
// P then X then Y, each 128 words; Pd[i]=(i*13+5)%n, Xd[i]=i*0.5-3+j; n=256, nj=4). Packing: two
// 32-bit elements per 64-bit word, element i in lane (i%2) — element0 low, element1 high (the
// array_utils write_array_slice contract).
//
// Cycle protocol (per the task): put(clk,0); run; sample kernel OUTPUTS while clk is low (registered
// outputs are stable and present the value that the *upcoming* rising edge will consummate); detect
// AXI beats (VALID && READY) off those sampled outputs + the held TB inputs; put(clk,1); run (the
// edge consummates the detected beats); then advance every channel FSM and drive the next held
// inputs. Sampling in the low phase (not after the rising edge) is essential — an AXIS master
// deasserts TVALID the cycle *after* a beat, so a post-edge sample would miss the token.
//
// Gate: PASSED test / exit 0 with Y[i] == X[P[i]] bit-exact, proven through the actual RTL.

#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

// ---------------------------------------------------------------------------
// Problem constants (must match interleaver_task_tb.cpp)
// ---------------------------------------------------------------------------
static const int  N        = 256;
static const int  NJ       = 8;   // >4 so steady-state period (slope) separates from fill/drain latency
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;              // bytes per word = 8
static const int  MEM_NW   = 8192;                    // total words
static const int  WORDS_PER_ARR = 128;               // get_nwords<64>(256) for a 32-bit element
static const long MAX_CYCLES = 2000000L;             // safety timeout -> FAIL

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }

// The golden element value X[j][k] and permutation P[i] (P is j-independent).
static int   Pidx(int i)        { return ((i * 13 + 5) % N); }
static float Xval(int j, int k) { return (float)((float)k * 0.5f - 3.0f + (float)j); }

// ---------------------------------------------------------------------------
// XSI value helpers (2-state: aVal = bits, bVal = 0). A 4-logicval buffer covers every port
// here (<=128 bits); XSI reads/writes only as many logicvals as the port width needs.
// ---------------------------------------------------------------------------
typedef s_xsi_vlog_logicval LV;

struct Dut {
    Xsi::Loader& x;
    explicit Dut(Xsi::Loader& xx) : x(xx) {}

    int port(const char* name) {
        int p = x.get_port_number(name);
        if (p < 0) { std::fprintf(stderr, "FATAL: port '%s' not found\n", name); std::exit(3); }
        return p;
    }
    void put1(int p, uint32_t b)              { LV v; v.aVal = b & 1u; v.bVal = 0; x.put_value(p, &v); }
    void putW(int p, uint64_t val)            { LV v[4]; for (int k=0;k<4;k++){ v[k].aVal=(uint32_t)(val>>(32*k)); v[k].bVal=0; } x.put_value(p, v); }
    void put128(int p, const uint32_t w[4])   { LV v[4]; for (int k=0;k<4;k++){ v[k].aVal=w[k]; v[k].bVal=0; } x.put_value(p, v); }
    uint32_t get1(int p)                       { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return v[0].aVal & 1u; }
    uint64_t getW(int p)                       { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return ((uint64_t)v[1].aVal<<32) | v[0].aVal; }
};

int main() {
    // -----------------------------------------------------------------------
    // 1) Build the shared memory exactly as interleaver_task_tb.cpp does.
    // -----------------------------------------------------------------------
    std::vector<uint64_t> mem(MEM_NW, 0);
    int yw[NJ];
    uint32_t cmd_words[NJ][4];   // packed 128-bit Cmd: {n, p_addr, x_addr, y_addr} low->high

    for (int j = 0; j < NJ; ++j) {
        int base = j * 3 * WORDS_PER_ARR;
        int pw = base, xw = base + WORDS_PER_ARR, yj = base + 2 * WORDS_PER_ARR;
        yw[j] = yj;
        for (int i = 0; i < N; ++i) {
            uint32_t pbits = (uint32_t)Pidx(i);
            uint32_t xbits = fbits(Xval(j, i));
            int      lane  = i & 1;
            uint64_t shift = (uint64_t)(lane * 32);
            uint64_t maskkeep = (lane == 0) ? 0xFFFFFFFF00000000ull : 0x00000000FFFFFFFFull;
            mem[pw + i/2] = (mem[pw + i/2] & maskkeep) | ((uint64_t)pbits << shift);
            mem[xw + i/2] = (mem[xw + i/2] & maskkeep) | ((uint64_t)xbits << shift);
        }
        cmd_words[j][0] = (uint32_t)N;
        cmd_words[j][1] = (uint32_t)(pw * BPW);
        cmd_words[j][2] = (uint32_t)(xw * BPW);
        cmd_words[j][3] = (uint32_t)(yj * BPW);
    }

    // -----------------------------------------------------------------------
    // 2) Open the elaborated design.
    // -----------------------------------------------------------------------
    std::string design = "xsim.dir/interleaver/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "il_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    // Port handles ---------------------------------------------------------
    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    // AXIS command (kernel = slave; TB = master)
    int P_cmd_data  = d.port("s_cmd_TDATA");
    int P_cmd_valid = d.port("s_cmd_TVALID");
    int P_cmd_ready = d.port("s_cmd_TREADY");
    // AXIS done (kernel = master; TB = slave)
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

    // Pin every other TB-driven input to 0 so no X propagates. s_axi_control regs default 0
    // (offset base 0 == the byte addresses in the commands), and the unused half of each bundle
    // (gmem0 write side, gmem1 read side) is held quiescent.
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

    // -----------------------------------------------------------------------
    // 3) Held TB-driven signal state + channel FSMs.
    // -----------------------------------------------------------------------
    // AXIS command master
    int      cmd_idx = 0;
    uint32_t h_cmd_valid = 0;
    // AXIS done slave
    int      done_count = 0;
    long     done_cyc[NJ];              // cycle each done token arrived -> per-job period = the slope
    const uint32_t h_done_ready = 1;    // always ready
    // gmem0 read-slave FSM
    enum { AR_IDLE, R_SEND } g0_state = AR_IDLE;
    uint64_t g0_addrw = 0; uint32_t g0_len = 0, g0_beat = 0;
    uint32_t h_g0_arready = 1, h_g0_rvalid = 0, h_g0_rlast = 0; uint64_t h_g0_rdata = 0;
    // gmem1 write-slave FSM
    enum { AW_IDLE, W_RECV, B_RESP } g1_state = AW_IDLE;
    uint64_t g1_addrw = 0; uint32_t g1_len = 0, g1_wbeat = 0;
    uint32_t h_g1_awready = 1, h_g1_wready = 0, h_g1_bvalid = 0;

    // driveAll(): push every held TB input onto the DUT (ap_clk excluded — toggled explicitly).
    auto driveAll = [&]() {
        if (cmd_idx < NJ) d.put128(P_cmd_data, cmd_words[cmd_idx]);
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

    // -----------------------------------------------------------------------
    // 4) Reset: hold ap_rst_n=0 for several cycles, then deassert.
    // -----------------------------------------------------------------------
    d.put1(P_rst_n, 0);
    h_cmd_valid = (cmd_idx < NJ) ? 1u : 0u;
    driveAll();
    for (int k = 0; k < 16; ++k) {
        d.put1(P_clk, 0); xsi.run(10);
        d.put1(P_clk, 1); xsi.run(10);
    }
    d.put1(P_rst_n, 1);
    driveAll();

    // -----------------------------------------------------------------------
    // 5) Cycle loop.
    // -----------------------------------------------------------------------
    long cyc = 0;
    bool timed_out = false;
    long drain = -1;                 // cycle at which the last done arrived; then keep clocking so the
    for (;;) {                        // final posted write burst lands (done can fire before B response).
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NJ && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        // --- low phase: sample kernel outputs (stable, present at the upcoming edge) ---
        d.put1(P_clk, 0); xsi.run(10);

        uint32_t s_cmd_ready = d.get1(P_cmd_ready);
        uint32_t s_done_val  = d.get1(P_done_valid);
        uint32_t g0_arvalid  = d.get1(P_g0_arvalid);
        uint64_t g0_araddr   = d.getW(P_g0_araddr);
        uint32_t g0_arlen    = (uint32_t)(d.getW(P_g0_arlen) & 0xFF);
        uint32_t g0_rready    = d.get1(P_g0_rready);
        uint32_t g1_awvalid  = d.get1(P_g1_awvalid);
        uint64_t g1_awaddr   = d.getW(P_g1_awaddr);
        uint32_t g1_awlen    = (uint32_t)(d.getW(P_g1_awlen) & 0xFF);
        uint32_t g1_wvalid   = d.get1(P_g1_wvalid);
        uint64_t g1_wdata    = d.getW(P_g1_wdata);
        uint32_t g1_wstrb    = (uint32_t)(d.getW(P_g1_wstrb) & 0xFF);
        uint32_t g1_wlast    = d.get1(P_g1_wlast);
        uint32_t g1_bready   = d.get1(P_g1_bready);

        // --- detect beats consummated on the coming rising edge (VALID && READY) ---
        bool cmd_beat   = (h_cmd_valid && s_cmd_ready);
        bool done_beat  = (s_done_val && h_done_ready);
        bool g0_ar_beat = (g0_state == AR_IDLE) && g0_arvalid && h_g0_arready;
        bool g0_r_beat  = (g0_state == R_SEND)  && h_g0_rvalid && g0_rready;
        bool g1_aw_beat = (g1_state == AW_IDLE) && g1_awvalid && h_g1_awready;
        bool g1_w_beat  = (g1_state == W_RECV)  && g1_wvalid && h_g1_wready;
        bool g1_b_beat  = (g1_state == B_RESP)  && h_g1_bvalid && g1_bready;

        // --- rising edge: kernel captures / advances ---
        d.put1(P_clk, 1); xsi.run(10);

        // --- apply beat effects, advance FSMs, compute next held inputs ---
        if (cmd_beat) { ++cmd_idx; h_cmd_valid = (cmd_idx < NJ) ? 1u : 0u; }
        if (done_beat) { done_cyc[done_count] = cyc; ++done_count; }

        // gmem0 read slave
        if (g0_ar_beat) {
            g0_addrw = g0_araddr / BPW; g0_len = g0_arlen; g0_beat = 0;
            g0_state = R_SEND;
            h_g0_arready = 0; h_g0_rvalid = 1;
            h_g0_rdata = mem[g0_addrw + 0]; h_g0_rlast = (g0_len == 0) ? 1u : 0u;
        } else if (g0_r_beat) {
            if (g0_beat >= g0_len) {   // last beat just accepted
                g0_state = AR_IDLE; h_g0_rvalid = 0; h_g0_rlast = 0; h_g0_arready = 1;
            } else {
                ++g0_beat;
                h_g0_rdata = mem[g0_addrw + g0_beat];
                h_g0_rlast = (g0_beat >= g0_len) ? 1u : 0u;
            }
        }

        // gmem1 write slave
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
        std::fprintf(stderr,
            "FAILED test: TIMEOUT after %ld cycles, done=%d/%d\n"
            "  s_cmd: idx=%d valid=%u | g0_state=%d addrw=%llu len=%u beat=%u\n"
            "  g1_state=%d addrw=%llu len=%u wbeat=%u | last held: arready=%u rvalid=%u awready=%u wready=%u bvalid=%u\n",
            cyc, done_count, NJ, cmd_idx, h_cmd_valid,
            (int)g0_state, (unsigned long long)g0_addrw, g0_len, g0_beat,
            (int)g1_state, (unsigned long long)g1_addrw, g1_len, g1_wbeat,
            h_g0_arready, h_g0_rvalid, h_g1_awready, h_g1_wready, h_g1_bvalid);
        xsi.close();
        return 1;
    }

    // -----------------------------------------------------------------------
    // 6) Golden check: every Y[i] == X[P[i]] bit-exact, unpacked from mem.
    // -----------------------------------------------------------------------
    int fails = 0;
    for (int j = 0; j < NJ; ++j) {
        for (int i = 0; i < N; ++i) {
            uint64_t w = mem[yw[j] + i/2];
            uint32_t got = (uint32_t)((w >> ((i & 1) * 32)) & 0xFFFFFFFFull);
            uint32_t exp = fbits(Xval(j, Pidx(i)));
            if (got != exp) {
                if (fails < 8)
                    std::fprintf(stderr, "  job %d elem %d: got 0x%08x exp 0x%08x\n", j, i, got, exp);
                ++fails;
            }
        }
    }

    long latency = (drain >= 0) ? drain : cyc;   // cycle the last done arrived (excludes post-done drain)
    std::printf("interleaver XSI BFM: n=%d nj=%d cycles=%ld done=%d/%d\n", N, NJ, latency, done_count, NJ);
    // Per-job done cycle + period (delta from previous). Steady-state period = throughput; job 0 = fill latency.
    std::printf("  per-job done cycles (period in parens):\n   ");
    for (int j = 0; j < done_count; ++j)
        std::printf(" j%d=%ld(%s%ld)", j, done_cyc[j],
                    j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
    std::printf("\n  bus floor ~= n/job for MEM_DW=64 (2 elems/word); 2n for MEM_DW=32\n");
    xsi.close();
    if (fails) {
        std::printf("FAILED test: %d mismatched elements\n", fails);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
