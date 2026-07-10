// sob_bfm_tb.cpp — pure-AXIS XSI BFM that RTL-verifies the free-running hls::task + stream_of_blocks
// interleaver (interleaver_sob_task.cpp) in xsim. NO m_axi — this isolates "does stream_of_blocks
// deadlock with hls::task in RTL?" from the ap_ctrl_none+m_axi confound, and it dodges Vitis cosim
// (unreliable for ap_ctrl_none). Three AXIS channels + clock:
//   x_in  (kernel slave, TB master):  NJ*N floats, X[j][k] = k*0.5 - 3 + j
//   p_in  (kernel slave, TB master):  NJ*N ints,   P[i]    = (i*13+5) % N   (per job)
//   y_out (kernel master, TB slave):  NJ*N floats, expect  Y[j][i] = X[j][P[i]]
// Cycle protocol identical to il_bfm_tb.cpp: sample kernel outputs in the clock-LOW phase (an AXIS
// master deasserts TVALID the cycle after a beat, so a post-edge sample would miss it), detect
// beats (VALID && READY), pulse the edge, then advance FSMs + drive next inputs. Reports cycle count
// so we can compare against the ping-pong overlap floor ~ (NJ+1)*N vs the serial NJ*2N.

#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_loader.h"

static const int  N  = 256;
static const int  NJ = 4;
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
    void put1(int p, uint32_t b)   { LV v; v.aVal = b & 1u; v.bVal = 0; x.put_value(p, &v); }
    void putW(int p, uint32_t val) { LV v; v.aVal = val; v.bVal = 0; x.put_value(p, &v); }
    uint32_t get1(int p)           { LV v[2]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return v[0].aVal & 1u; }
    uint32_t getW(int p)           { LV v[2]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return v[0].aVal; }
};

int main() {
    // Golden streams (flattened NJ*N).
    std::vector<uint32_t> x_stream(NJ * N), p_stream(NJ * N), y_expect(NJ * N);
    for (int j = 0; j < NJ; ++j)
        for (int i = 0; i < N; ++i) {
            x_stream[j*N + i] = fbits(Xval(j, i));
            p_stream[j*N + i] = (uint32_t)Pidx(i);
            y_expect[j*N + i] = fbits(Xval(j, Pidx(i)));
        }

    std::string design = "xsim.dir/interleaver_sob_task/xsimk.dll";
    std::string engine = "xv_simulator_kernel.dll";
    Xsi::Loader xsi(design, engine);
    s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
    char wdb[] = "sob_bfm.wdb"; info.wdbFileName = wdb;
    xsi.open(&info);
    Dut d(xsi);

    int P_clk   = d.port("ap_clk");
    int P_rst_n = d.port("ap_rst_n");
    int P_x_data  = d.port("x_in_TDATA");
    int P_x_valid = d.port("x_in_TVALID");
    int P_x_ready = d.port("x_in_TREADY");
    int P_p_data  = d.port("p_in_TDATA");
    int P_p_valid = d.port("p_in_TVALID");
    int P_p_ready = d.port("p_in_TREADY");
    int P_y_data  = d.port("y_out_TDATA");
    int P_y_valid = d.port("y_out_TVALID");
    int P_y_ready = d.port("y_out_TREADY");

    // Held TB-driven state.
    int x_idx = 0, p_idx = 0, y_idx = 0;
    const uint32_t h_y_ready = 1;              // TB always ready to accept Y
    std::vector<uint32_t> y_got(NJ * N, 0);

    auto driveAll = [&]() {
        d.putW(P_x_data,  x_idx < NJ*N ? x_stream[x_idx] : 0);
        d.put1(P_x_valid, x_idx < NJ*N ? 1u : 0u);
        d.putW(P_p_data,  p_idx < NJ*N ? p_stream[p_idx] : 0);
        d.put1(P_p_valid, p_idx < NJ*N ? 1u : 0u);
        d.put1(P_y_ready, h_y_ready);
    };

    // Reset.
    d.put1(P_rst_n, 0);
    driveAll();
    for (int k = 0; k < 16; ++k) { d.put1(P_clk,0); xsi.run(10); d.put1(P_clk,1); xsi.run(10); }
    d.put1(P_rst_n, 1);
    driveAll();

    long cyc = 0; bool timed_out = false;
    for (;;) {
        if (y_idx >= NJ*N) break;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        // low phase: sample kernel outputs.
        d.put1(P_clk, 0); xsi.run(10);
        uint32_t x_ready = d.get1(P_x_ready);
        uint32_t p_ready = d.get1(P_p_ready);
        uint32_t y_valid = d.get1(P_y_valid);
        uint32_t y_data  = d.getW(P_y_data);

        // detect beats consummated at the coming edge.
        bool x_beat = (x_idx < NJ*N) && x_ready;
        bool p_beat = (p_idx < NJ*N) && p_ready;
        bool y_beat = y_valid && h_y_ready;

        // rising edge.
        d.put1(P_clk, 1); xsi.run(10);

        if (x_beat) ++x_idx;
        if (p_beat) ++p_idx;
        if (y_beat && y_idx < NJ*N) y_got[y_idx++] = y_data;

        driveAll();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT after %ld cycles, y=%d/%d (x=%d p=%d)\n",
                     cyc, y_idx, NJ*N, x_idx, p_idx);
        xsi.close();
        return 1;
    }

    int fails = 0;
    for (int k = 0; k < NJ*N; ++k) {
        if (y_got[k] != y_expect[k]) {
            if (fails < 8) std::fprintf(stderr, "  elem %d (job %d,%d): got 0x%08x exp 0x%08x\n",
                                        k, k/N, k%N, y_got[k], y_expect[k]);
            ++fails;
        }
    }
    std::printf("sob_task XSI BFM: n=%d nj=%d cycles=%ld  (serial~%d, overlap_floor~%d)\n",
                N, NJ, cyc, NJ*2*N, (NJ+1)*N);
    xsi.close();
    if (fails) { std::printf("FAILED test: %d mismatched elements\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
