// gemv_top.cpp -- synthesis tops.  Thin wrappers so each DUT is one named function for
// csynth / cosim; all the arithmetic is the library's, unmodified.
//
// The stream plumbing lives inside each top, mirroring the library's own testbench
// (L1/tests/hw/gemv/uut_top.cpp) -- including its use of the shipped data movers.  gemv consumes
// the vector once PER ROW, so vec2GemStream (which repeats x for each row) is required; a
// hand-fed x starves and the kernel blocks forever rather than erroring.
#include "gemv_top.hpp"

using namespace xf::blas;

// Every 5-arg DUT is the same six lines at a different type and size, so they are generated
// rather than copied -- a copy would drift.
#define WF_GEMV_TOP(NAME, T, M, N, LOGP)                                        \
    void NAME(T p_A[(M) * (N)], T p_x[N], T p_y[M]) {                           \
        _Pragma("HLS DATAFLOW")                                                 \
        hls::stream<WideType<T, (1 << (LOGP))>::t_TypeInt> l_strA, l_strX;      \
        hls::stream<WideType<T, 1>::t_TypeInt> l_strY;                          \
        gem2Stream<T, (1 << (LOGP))>((M), (N), p_A, l_strA);                    \
        vec2GemStream<T, (1 << (LOGP))>((M), (N), p_x, l_strX);                 \
        gemv<T, (LOGP), unsigned int>((M), (N), l_strA, l_strX, l_strY);        \
        writeStream2Vec<T, 1>(l_strY, (M), p_y);                                \
    }

WF_GEMV_TOP(gemv_f32_top,      T_f32,   GEMV_M,    GEMV_N,    GEMV_LOGP)
WF_GEMV_TOP(gemv_f32_wide_top, T_f32,   GEMVW_M,   GEMVW_N,   GEMVW_LOGP)
WF_GEMV_TOP(gemv_f32_pad_top,  T_f32,   GEMVPD_M,  GEMVPD_N,  GEMVPD_LOGP)
WF_GEMV_TOP(gemv_f64_top,      T_f64,   GEMVD_M,   GEMVD_N,   GEMVD_LOGP)
WF_GEMV_TOP(gemv_i32_top,      T_i32,   GEMVI_M,   GEMVI_N,   GEMVI_LOGP)
WF_GEMV_TOP(gemv_u32_top,      T_u32,   GEMVI_M,   GEMVI_N,   GEMVI_LOGP)
WF_GEMV_TOP(gemv_fixed_top,    T_fixed, GEMVF_M,   GEMVF_N,   GEMVI_LOGP)
WF_GEMV_TOP(gemv_fix24_top,    T_fix24, GEMVF24_M, GEMVF24_N, GEMVI_LOGP)

// The "bring your own input" DUT.  Its sizes and element type come from gemv_user_cfg.hpp,
// which make_user_dut.py generates from the header of data/user_input.txt.
WF_GEMV_TOP(gemv_user_top,     T_user,  WF_USER_M, WF_USER_N, WF_USER_LOGP)

// The alpha/beta overload has a different shape: two more scalars, a second input vector read
// through readVec2Stream, and a second output stream.
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
