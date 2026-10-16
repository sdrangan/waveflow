// rotate_ref.h -- the grader's own rotate kernel, written to validate the grader.
//
// One transaction per call.  Layout (our choice; rotate.md leaves it open):
//   WORD_BW=32: word 0 = tx_id[15:0] | len[31:16];  word 1 = cos[9:0] | sin[25:16]
//   WORD_BW=64: word 0 = tx_id[15:0] | len[31:16] | cos[41:32] | sin[57:48]
//   data: 16-bit Q16.8 lanes, x then y, lane 0 in the low bits; TLAST on the last word
//   output: the same lane packing, no header, TLAST on the last word
//   arithmetic: x*cos - y*sin and x*sin + y*cos in full precision, round half up, saturate
//   status: 0 = ok, 1 = TLAST before the last data word (the kernel stops reading)
#pragma once
#include <ap_axi_sdata.h>
#include <ap_int.h>
#include <hls_stream.h>

typedef ap_axiu<32, 0, 0, 0> rot_word32_t;
typedef ap_axiu<64, 0, 0, 0> rot_word64_t;

void rot32(hls::stream<rot_word32_t>& in, hls::stream<rot_word32_t>& out, ap_uint<8>& status);
void rot64(hls::stream<rot_word64_t>& in, hls::stream<rot_word64_t>& out, ap_uint<8>& status);
