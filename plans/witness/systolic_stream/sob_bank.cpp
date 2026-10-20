// Can a stream_of_blocks block be banked, so its reader takes R values per cycle?
#include "hls_task.h"
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
const int R = 4, K = 8;
typedef ap_uint<24> v_t;
typedef v_t blk_t[R][K];

static void prod(hls::stream<ap_uint<64> >& in, hls::stream_of_blocks<blk_t, 2>& b) {
    hls::write_lock<blk_t> w(b);
WR:
    for (int x = 0; x < R * K; ++x) {
#pragma HLS PIPELINE II = 1
        w[x % R][x / R] = (v_t)in.read();
    }
}

static void cons(hls::stream_of_blocks<blk_t, 2>& b, hls::stream<ap_uint<64> >& out) {
    hls::read_lock<blk_t> r(b);
    ap_uint<64> acc = 0;
RD:
    for (int k = 0; k < K; ++k) {
#pragma HLS PIPELINE II = 1
        for (int i = 0; i < R; ++i) {
#pragma HLS UNROLL
            acc += r[i][k] * (i + 1);
        }
    }
    out.write(acc);
}

void sob_bank(hls::stream<ap_uint<64> >& in, hls::stream<ap_uint<64> >& out) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE ap_ctrl_none port = return
    hls_thread_local hls::stream_of_blocks<blk_t, 2> b;
#pragma HLS ARRAY_PARTITION variable = b dim = 1 complete
    hls_thread_local hls::task t0(prod, in, b);
    hls_thread_local hls::task t1(cons, b, out);
}
