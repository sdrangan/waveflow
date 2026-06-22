// block_scale_compute_impl.cpp
// Hand-written pure-compute hook for the block_scale example.
//
// The array generalization of examples/regmap/simp_fun_compute_impl.cpp: the
// I/O (the m_axi operand/result bursts) is auto-generated in run_proc, so this
// hook is *pure compute* over the materialized C++ array — it never touches the
// port.  y[i] = A*x[i] + B with A=3, B=-4 over the first n of MAX_N=256
// elements; full precision in ap_int<32>, matching the Python golden
// block_affine().

#include <ap_int.h>

namespace block_scale_impl {

void compute(ap_int<32> x[256], int n, ap_int<32> out[256]) {
#pragma HLS INLINE
    for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
        out[i] = ap_int<32>(3) * x[i] + ap_int<32>(-4);
    }
}

} // namespace block_scale_impl
