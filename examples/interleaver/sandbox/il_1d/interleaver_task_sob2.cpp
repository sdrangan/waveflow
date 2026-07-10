// interleaver_task_sob2.cpp — the OVERLAPPING free-running interleaver with MINIMAL-buffering AXI<->stream
// adapters (the user's "five processes"). Contrast with interleaver_task_sob.cpp, which was correct
// (XSI done=4/4) but measured 3686 (~no overlap) because its load/store used read/write_array_slice =
// buffer-ALL-then-stream, injecting a full-n serial dependency the fill<->gather ping-pong couldn't hide.
//
// Here the edge adapters stream element-by-element as the burst flows (one word resident), so load(j+1)
// truly overlaps gather(j). The two adapters (a2s / s2a) are the reusable memory-endpoint components —
// they own the m_axi and provide the arbitration; every compute task is pure stream/block.
//
//   s_cmd -> load[a2s x2] ==(x_s,p_s)==> fill ==(x_blk sob d2)==> gather ==(y_s)==> store[s2a] -> s_done
//              |owns in_mem(R)                                                      |owns out_mem(W)
//
// The interleaver is a pure gather (Y[i]=X[P[i]], no arithmetic), so we carry raw ap_uint<32> bits
// throughout — the adapters are element-type-agnostic. Packing = write_array_slice contract:
// 2x 32-bit elems per 64-bit word, elem i in lane (i%2), low then high (MEM_DW=64).
#include "hls_task.h"
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "memmgr.hpp"

#ifndef MEM_DW
#define MEM_DW 64
#endif
static const int N_MAX = 1024;
typedef ap_uint<32> xblock_t[N_MAX];
typedef ap_uint<32> word_t;

#include "cmd.h"

// AXI->stream adapter: burst n 32-bit elems from base[], streaming each straight out (one word resident).
static void a2s(const ap_uint<MEM_DW>* base, int n, hls::stream<word_t>& out) {
    const int nw = (n + 1) >> 1;
A2S: for (int w = 0; w < nw; ++w) {
        ap_uint<MEM_DW> word = base[w];              // sequential -> Vitis infers a burst read
        out.write(word.range(31, 0));
        if (((w << 1) + 1) < n) out.write(word.range(63, 32));
    }
}
// stream->AXI adapter: pull n 32-bit elems, pack 2/word, pure-write burst (one word resident).
static void s2a(ap_uint<MEM_DW>* base, int n, hls::stream<word_t>& in) {
    const int nw = (n + 1) >> 1;
S2A: for (int w = 0; w < nw; ++w) {
        ap_uint<MEM_DW> word = 0;
        word.range(31, 0) = in.read();
        if (((w << 1) + 1) < n) word.range(63, 32) = in.read();
        base[w] = word;                               // sequential pure-write -> burst
    }
}

// LOAD: sole read-m_axi owner. Two minimal a2s adapters (P then X), both on gmem0.
static void load_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                      hls::stream<word_t>& x_s, hls::stream<word_t>& p_s,
                      const ap_uint<MEM_DW>* in_mem) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int n  = (int)c.n;
    const int pw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.p_addr);
    const int xw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.x_addr);
    a2s(in_mem + pw, n, p_s);    // p_s is deep (see top): load dumps P and moves to X without blocking
    a2s(in_mem + xw, n, x_s);
}

// FILL: pure stream->block, ping-pong producer.
static void fill_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                      hls::stream<word_t>& x_s, hls::stream_of_blocks<xblock_t>& x_blk) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int n = (int)c.n;
    hls::write_lock<xblock_t> b(x_blk);
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        b[i] = x_s.read();
    }
}

// GATHER: ping-pong consumer, random-access by streamed index.
static void gather_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                        hls::stream_of_blocks<xblock_t>& x_blk, hls::stream<word_t>& p_s,
                        hls::stream<word_t>& y_s) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int n = (int)c.n;
    hls::read_lock<xblock_t> b(x_blk);
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        y_s.write(b[(int)p_s.read()]);
    }
}

// STORE: sole write-m_axi owner, minimal s2a adapter.
static void store_task(hls::stream<Cmd>& cmd_in, hls::stream<word_t>& y_s,
                       hls::stream<ap_uint<32> >& done, ap_uint<MEM_DW>* out_mem) {
    Cmd c = cmd_in.read();
    const int n  = (int)c.n;
    const int yw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.y_addr);
    s2a(out_mem + yw, n, y_s);
    done.write(c.n);
}

void interleaver(hls::stream<Cmd>& s_cmd, hls::stream<ap_uint<32> >& s_done,
                 const ap_uint<MEM_DW>* in_mem, ap_uint<MEM_DW>* out_mem) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE axis port=s_done
#pragma HLS INTERFACE m_axi port=in_mem  offset=slave bundle=gmem0 depth=8192
#pragma HLS INTERFACE m_axi port=out_mem offset=slave bundle=gmem1 depth=8192
#pragma HLS INTERFACE ap_ctrl_none port=return
#pragma HLS stable variable=in_mem
    hls_thread_local hls::stream<Cmd>                c_lf, c_fg, c_gs;
    hls_thread_local hls::stream<word_t>             x_s, y_s;
    hls_thread_local hls::stream<word_t>             p_s;
#pragma HLS STREAM variable=p_s depth=1024
    hls_thread_local hls::stream_of_blocks<xblock_t, 2> x_blk;
    hls_thread_local hls::task t_load (load_task,   s_cmd, c_lf, x_s, p_s, in_mem);
    hls_thread_local hls::task t_fill (fill_task,   c_lf, c_fg, x_s, x_blk);
    hls_thread_local hls::task t_gath (gather_task, c_fg, c_gs, x_blk, p_s, y_s);
    hls_thread_local hls::task t_store(store_task,  c_gs, y_s, s_done, out_mem);
}
