// wf_lanes.h -- lane groups: how the linear-algebra components hold a matrix in a block.
//
// A matrix is held as groups of L complex values, row-major, so L columns are touched per cycle.
// Lane l of a group sits at bits [2W*l, 2W*l + 2W): the real part in the low W bits and the
// imaginary part in the high W bits, the ComplexField bit order.  The helpers convert between a
// group and L (re, im) register values; the conversion reinterprets stored bits, never computes.
// Python twin: waveflow/linalg/lanes.py (pack_group, unpack_group).
#ifndef WF_LANES_H
#define WF_LANES_H
#include <ap_int.h>

namespace wf_lanes {

template <class T, int L>
struct group {
    typedef ap_uint<2 * T::width * L> type;
};

// The L lanes of a group.
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

// A group from L lanes.
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

}  // namespace wf_lanes

#endif  // WF_LANES_H
