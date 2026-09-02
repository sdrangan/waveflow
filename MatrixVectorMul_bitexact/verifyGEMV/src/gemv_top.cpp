// gemv_top.cpp -- synthesis tops.  Thin wrappers so each DUT is one named function for
// csynth / cosim; all the arithmetic is the library's, unmodified.
//
// The stream plumbing lives inside each top, mirroring the library's own testbench
// (L1/tests/hw/gemv/uut_top.cpp) -- including its use of the shipped data movers.  gemv consumes
// the vector once PER ROW, so vec2GemStream (which repeats x for each row) is required; a
// hand-fed x starves and the kernel blocks forever rather than erroring.
#include "gemv_top.hpp"

using namespace xf::blas;

void gemv_f32_top(T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N], T_f32 p_y[GEMV_M]) {
#pragma HLS DATAFLOW
    hls::stream<WideType<T_f32, GEMV_P>::t_TypeInt> l_strA, l_strX;
    hls::stream<WideType<T_f32, 1>::t_TypeInt> l_strY;
    gem2Stream<T_f32, GEMV_P>(GEMV_M, GEMV_N, p_A, l_strA);
    vec2GemStream<T_f32, GEMV_P>(GEMV_M, GEMV_N, p_x, l_strX);
    gemv<T_f32, GEMV_LOGP, unsigned int>(GEMV_M, GEMV_N, l_strA, l_strX, l_strY);
    writeStream2Vec<T_f32, 1>(l_strY, GEMV_M, p_y);
}

void gemv_ab_top(T_f32 p_alpha, T_f32 p_beta,
                 T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N],
                 T_f32 p_y[GEMV_M], T_f32 p_yr[GEMV_M]) {
#pragma HLS DATAFLOW
    hls::stream<WideType<T_f32, GEMV_P>::t_TypeInt> l_strA, l_strX;
    hls::stream<WideType<T_f32, 1>::t_TypeInt> l_strY, l_strYR;
    gem2Stream<T_f32, GEMV_P>(GEMV_M, GEMV_N, p_A, l_strA);
    vec2GemStream<T_f32, GEMV_P>(GEMV_M, GEMV_N, p_x, l_strX);
    readVec2Stream<T_f32, 1>(p_y, GEMV_M, l_strY);
    gemv<T_f32, GEMV_LOGP, unsigned int>(GEMV_M, GEMV_N, p_alpha, l_strA, l_strX, p_beta,
                                         l_strY, l_strYR);
    writeStream2Vec<T_f32, 1>(l_strYR, GEMV_M, p_yr);
}

void gemv_fixed_top(T_fixed p_A[GEMVF_M * GEMVF_N], T_fixed p_x[GEMVF_N], T_fixed p_y[GEMVF_M]) {
#pragma HLS DATAFLOW
    hls::stream<WideType<T_fixed, GEMV_P>::t_TypeInt> l_strA, l_strX;
    hls::stream<WideType<T_fixed, 1>::t_TypeInt> l_strY;
    gem2Stream<T_fixed, GEMV_P>(GEMVF_M, GEMVF_N, p_A, l_strA);
    vec2GemStream<T_fixed, GEMV_P>(GEMVF_M, GEMVF_N, p_x, l_strX);
    gemv<T_fixed, GEMV_LOGP, unsigned int>(GEMVF_M, GEMVF_N, l_strA, l_strX, l_strY);
    writeStream2Vec<T_fixed, 1>(l_strY, GEMVF_M, p_y);
}
