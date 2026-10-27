// w5d.cpp -- witness 5, control d: the documented scoped locks, same structure: queue blocks acquired and released by decoded instructions.
//
// Three free-running hls::tasks: `fill` lands 64 lane groups a block on `qa`; `exec` runs a program --
// COPY g (copy column group g of the head block of qa, plus one, to the tail block of qb, acquiring
// each block on first use), POP (release qa's head), PUSH (commit qb's tail) -- through
// hls::read_buf / hls::write_buf, whose acquire() and release() are called under run-time control;
// `drain` streams each committed block out. The question: does csynth take it, at II 1 in the COPY
// loop, and does the RTL agree with C (tb5.cpp)?
#include <ap_int.h>
#include <hls_stream.h>
#include <hls_streamofblocks.h>
#include <hls_task.h>

typedef ap_uint<96> lg_t;              // a lane group: 4 complex 12-bit values
typedef lg_t blk_t[64];                // a block: 8 rows x 8 column groups
typedef ap_uint<64> word_t;

enum { OP_COPY = 0, OP_POP = 1, OP_PUSH = 2 };

static void fill(hls::stream<lg_t>& in, hls::stream_of_blocks<blk_t>& qa) {
    hls::write_lock<blk_t> b(qa);
FILL:
    for (int i = 0; i < 64; ++i) {
#pragma HLS PIPELINE II=1
        b[i] = in.read();
    }
}

static void exec(hls::stream<word_t>& prog, hls::stream_of_blocks<blk_t>& qa,
                 hls::stream_of_blocks<blk_t>& qb, hls::stream<word_t>& status) {
        const word_t job = prog.read();
    const int n = job(31, 16);
BLOCKS:
    for (int blk = 0; blk < n / 10; ++blk) {   // 8 COPY, a POP and a PUSH a block
#pragma HLS LOOP_TRIPCOUNT max=4
        hls::read_lock<blk_t> src(qa);
        hls::write_lock<blk_t> dst(qb);
    GROUPS:
        for (int k = 0; k < 10; ++k) {
            const word_t w = prog.read();
            const int op = w(6, 0), g = w(51, 40);
            if (op == OP_COPY) {
            COPY:
                for (int i = 0; i < 8; ++i) {
#pragma HLS PIPELINE II=1
                    dst[i * 8 + g] = src[i * 8 + g] + 1;
                }
            }
        }
    }
    status.write(job(15, 0));
}

static void drain(hls::stream_of_blocks<blk_t>& qb, hls::stream<lg_t>& out) {
    hls::read_lock<blk_t> b(qb);
DRAIN:
    for (int i = 0; i < 64; ++i) {
#pragma HLS PIPELINE II=1
        out.write(b[i]);
    }
}

void w5_top(hls::stream<lg_t>& in, hls::stream<word_t>& prog, hls::stream<lg_t>& out,
            hls::stream<word_t>& status) {
#pragma HLS INTERFACE mode=ap_ctrl_none port=return
    hls_thread_local hls::stream_of_blocks<blk_t> qa, qb;
    hls_thread_local hls::task t1(fill, in, qa);
    hls_thread_local hls::task t2(exec, prog, qa, qb, status);
    hls_thread_local hls::task t3(drain, qb, out);
}
