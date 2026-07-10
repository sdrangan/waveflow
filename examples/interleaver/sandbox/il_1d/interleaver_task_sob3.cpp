// interleaver_task_sob3.cpp — WORD-GRANULAR, max-throughput free-running interleaver. Fixes sob2's
// throughput loss (measured 537 cyc/job ~= 2n at MEM_DW=64, because sob2's a2s serialized each 64-bit
// word into two 32-bit stream writes -> element-rate, half bus bandwidth). Here EVERY stream/block is
// ap_uint<MEM_DW> (word rate), and the ONLY place lanes are touched is the gather, which unrolls by
// LW = MEM_DW/32 (2 for 64b): read one packed index-word, do LW random block-word reads, pack LW
// results into one output word. All stages run at n/LW words/job -> the true bus floor.
//
//   s_cmd -> load[a2s x2] ==(x_s,p_s: words)==> fill ==(x_blk: word block, sob d2)==> gather[unpack/pack
//              |owns in_mem(R)                                   LW]==(y_s: words)==> store[s2a] -> s_done
//
// Packing (BFM / write_array_slice contract): LW 32-bit elems per MEM_DW word, elem i in lane (i%LW).
#include "hls_task.h"
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "memmgr.hpp"

#ifndef MEM_DW
#define MEM_DW 64
#endif
static const int LW      = MEM_DW / 32;         // 32-bit lanes per word (2 for MEM_DW=64)
static const int NW_MAX  = 1024;                // max resident words in the block
typedef ap_uint<MEM_DW> word_t;
typedef word_t          wblock_t[NW_MAX];

#include "cmd.h"

static inline int nwords(int n) { return (n + LW - 1) / LW; }

// AXI->stream: pass nw words straight through, no unpack. Word rate (1 word/cycle, burst inferred).
static void a2s(const ap_uint<MEM_DW>* base, int nw, hls::stream<word_t>& out) {
A2S: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        out.write(base[w]);
    }
}
// stream->AXI: pure-write nw words. Word rate.
static void s2a(ap_uint<MEM_DW>* base, int nw, hls::stream<word_t>& in) {
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        base[w] = in.read();
    }
}

// LOAD: sole read-m_axi owner. P words then X words (both word rate).
static void load_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                      hls::stream<word_t>& x_s, hls::stream<word_t>& p_s,
                      const ap_uint<MEM_DW>* in_mem) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int nw = nwords((int)c.n);
    const int pw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.p_addr);
    const int xw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.x_addr);
    a2s(in_mem + pw, nw, p_s);      // p_s deep (see top): load dumps P and moves to X
    a2s(in_mem + xw, nw, x_s);
}

// FILL: x_s words -> resident word block, ping-pong producer. Word rate.
static void fill_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                      hls::stream<word_t>& x_s, hls::stream_of_blocks<wblock_t>& x_blk) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int nw = nwords((int)c.n);
    hls::write_lock<wblock_t> b(x_blk);
    for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        b[w] = x_s.read();
    }
}

// GATHER: LW outputs/cycle. One packed index-word -> LW random block-word reads -> one packed out-word.
static void gather_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                        hls::stream_of_blocks<wblock_t>& x_blk, hls::stream<word_t>& p_s,
                        hls::stream<word_t>& y_s) {
    Cmd c = cmd_in.read();
    cmd_fwd.write(c);
    const int nw = nwords((int)c.n);
    hls::read_lock<wblock_t> b(x_blk);
GY: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        word_t pword = p_s.read();
        word_t yword = 0;
        for (int l = 0; l < LW; ++l) {
#pragma HLS UNROLL
            int idx        = (int)pword.range(32 * l + 31, 32 * l);   // output elem index P[w*LW+l]
            word_t xw      = b[idx / LW];                             // random block-word read (LW/cycle)
            ap_uint<32> xv = xw.range(32 * (idx % LW) + 31, 32 * (idx % LW));
            yword.range(32 * l + 31, 32 * l) = xv;
        }
        y_s.write(yword);
    }
}

// STORE: sole write-m_axi owner. y_s words -> mem. Word rate.
static void store_task(hls::stream<Cmd>& cmd_in, hls::stream<word_t>& y_s,
                       hls::stream<ap_uint<32> >& done, ap_uint<MEM_DW>* out_mem) {
    Cmd c = cmd_in.read();
    const int nw = nwords((int)c.n);
    const int yw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.y_addr);
    s2a(out_mem + yw, nw, y_s);
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
    hls_thread_local hls::stream_of_blocks<wblock_t, 2> x_blk;
    hls_thread_local hls::task t_load (load_task,   s_cmd, c_lf, x_s, p_s, in_mem);
    hls_thread_local hls::task t_fill (fill_task,   c_lf, c_fg, x_s, x_blk);
    hls_thread_local hls::task t_gath (gather_task, c_fg, c_gs, x_blk, p_s, y_s);
    hls_thread_local hls::task t_store(store_task,  c_gs, y_s, s_done, out_mem);
}
