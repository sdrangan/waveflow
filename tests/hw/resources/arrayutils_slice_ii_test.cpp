// Isolated m_axi kernel that exercises read_array_slice / write_array_slice over the bus, so
// csynth reports the slice loops' *achieved* initiation interval (II). csim is II-blind, so the
// existing slice conformance test (set_top main, local arrays) cannot guard pipelining/burst
// behavior -- it passed even when the scalar write path serialized to II=16 (the partial-word RMW
// read on the gmem bundle) and when a non-inlined dispatch left the m_axi port dangling. This top
// is the regression guard: the slice loops must pipeline at II=1.
//
// Read [0, N) into a local buffer, then write it back to a disjoint region [N, 2N): the two ranges
// stay live (no dead-code elimination) and aligned (N is a multiple of LW), so the report shows the
// bulk-throughput II of each direction.
#include "__HEADER__"

namespace au = __NAMESPACE__;

void slice_ii_top(ap_uint<__WORD_BW__>* mem) {
#pragma HLS INTERFACE m_axi port=mem offset=slave bundle=gmem \
    max_read_burst_length=256 max_write_burst_length=256 depth=__DEPTH__
#pragma HLS INTERFACE s_axilite port=return
    au::value_type buf[__N__];
    au::read_array_slice<__WORD_BW__>(mem, 0, __N__, buf);
    au::write_array_slice<__WORD_BW__>(buf, mem, __N__, 2 * __N__);
}
