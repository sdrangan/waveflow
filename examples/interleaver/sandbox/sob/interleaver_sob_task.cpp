// interleaver_sob_task.cpp — Gemini's canonical form: a free-running (ap_ctrl_none) hls::task network
// with hls::stream_of_blocks, PURE AXIS (NO m_axi). This isolates the question "does stream_of_blocks
// deadlock with hls::task?" from the m_axi confound (the il_1d rework had m_axi in the tasks and
// deadlocked in RTL; ap_ctrl_none+m_axi is independently fragile). Single-firing tasks (the runtime
// re-fires them); write_lock/read_lock scoped to the loop; P streamed in-loop. XSI-verified (not Vitis
// cosim, which is unreliable for ap_ctrl_none).
//
//   x_in(AXIS) -> load_task ==(x_blk: stream_of_blocks depth 2)==> gather_task -> y_out(AXIS)
//   p_in(AXIS) --------------------------------------------------->/
//
#include "hls_task.h"
#include "hls_stream.h"
#include "hls_streamofblocks.h"

static const int N = 256;
typedef float xblock_t[N];

// LOAD: fill a whole block from the x_in stream, then release it (the '}' scope frees it immediately).
static void load_task(hls::stream<float>& x_in, hls::stream_of_blocks<xblock_t>& x_blk) {
    hls::write_lock<xblock_t> b(x_blk);
    for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        b[i] = x_in.read();
    }
}

// GATHER: read-lock the filled block, random-access it with indices streamed on p_in.
static void gather_task(hls::stream<int>& p_in, hls::stream_of_blocks<xblock_t>& x_blk,
                        hls::stream<float>& y_out) {
    hls::read_lock<xblock_t> b(x_blk);
    for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        y_out.write(b[p_in.read()]);
    }
}

// TOP: free-running. The two tasks + the depth-2 block channel that ping-pongs between them.
void interleaver_sob_task(hls::stream<float>& x_in, hls::stream<int>& p_in,
                          hls::stream<float>& y_out) {
#pragma HLS INTERFACE axis port=x_in
#pragma HLS INTERFACE axis port=p_in
#pragma HLS INTERFACE axis port=y_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream_of_blocks<xblock_t, 2> x_blk;
    hls_thread_local hls::task t_load(load_task, x_in, x_blk);
    hls_thread_local hls::task t_gath(gather_task, p_in, x_blk, y_out);
}
