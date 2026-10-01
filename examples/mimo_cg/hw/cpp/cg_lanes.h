// cg_lanes.h -- lane groups: the stream-of-blocks element of every CG hardware block.
//
// A block holds one K x N register matrix as K*N/L lane groups (row k, columns c*L .. c*L+L-1),
// group index k*(N/L) + c.  Lane l of a group sits at bits [2W*l, 2W*l + 2W), re in the low W bits
// and im in the high W bits: the ComplexField bit order.  These helpers convert between a group and
// L (re, im) register values; the conversion is a reinterpretation of stored bits, not arithmetic.
// Python twin: examples/mimo_cg/hw/vec.py (block_type), which carries the same values as integers.
#ifndef MIMO_CG_LANES_H
#define MIMO_CG_LANES_H
#include <ap_int.h>

namespace cg_lanes {

template <class T, int L>
struct group {
    typedef ap_uint<2 * T::width * L> type;
};

template <class T, int L>
static inline void unpack(const typename group<T, L>::type& g, T re[L], T im[L]) {
#pragma HLS INLINE
    const int W = T::width;
    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
        re[l].range(W - 1, 0) = g.range(2 * W * l + W - 1, 2 * W * l);
        im[l].range(W - 1, 0) = g.range(2 * W * l + 2 * W - 1, 2 * W * l + W);
    }
}

template <class T, int L>
static inline typename group<T, L>::type pack(const T re[L], const T im[L]) {
#pragma HLS INLINE
    const int W = T::width;
    typename group<T, L>::type g = 0;
    for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
        g.range(2 * W * l + W - 1, 2 * W * l) = re[l].range(W - 1, 0);
        g.range(2 * W * l + 2 * W - 1, 2 * W * l + W) = im[l].range(W - 1, 0);
    }
    return g;
}

// One lane of a group.
template <class T, int L>
static inline void get(const typename group<T, L>::type& g, int l, T& re, T& im) {
#pragma HLS INLINE
    const int W = T::width;
    re.range(W - 1, 0) = g.range(2 * W * l + W - 1, 2 * W * l);
    im.range(W - 1, 0) = g.range(2 * W * l + 2 * W - 1, 2 * W * l + W);
}

// Set one lane of a group.
template <class T, int L>
static inline void set(typename group<T, L>::type& g, int l, const T& re, const T& im) {
#pragma HLS INLINE
    const int W = T::width;
    g.range(2 * W * l + W - 1, 2 * W * l) = re.range(W - 1, 0);
    g.range(2 * W * l + 2 * W - 1, 2 * W * l + W) = im.range(W - 1, 0);
}

}  // namespace cg_lanes

#endif  // MIMO_CG_LANES_H
