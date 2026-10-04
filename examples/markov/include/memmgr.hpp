#ifndef WAVEFLOW_BUILD_MEMMGR_HPP
#define WAVEFLOW_BUILD_MEMMGR_HPP

#include <ap_int.h>

namespace waveflow {
namespace memmgr {

/**
 * @brief Return whether a byte address is aligned to a packed word boundary.
 *
 * @tparam word_dwidth Word width in bits.
 * @tparam AddrT Integer-like address type.
 * @param addr Byte address to test.
 * @return ``true`` when ``addr`` is aligned to ``word_dwidth / 8`` bytes.
 */
template <int word_dwidth>
inline bool is_word_aligned(ap_uint<word_dwidth> addr) {
#pragma HLS INLINE
    return (addr % (word_dwidth / 8)) == 0;
}

/**
 * @brief Convert a byte address into a packed-word index.
 *
 * This helper is synthesizable and intended for HLS modules that expose a byte
 * address in command metadata but index into a word-addressed AXI memory port.
 *
 * @tparam word_dwidth Word width in bits.
 * @tparam AddrT Integer-like address type.
 * @param addr Byte address to convert.
 * @return Word index corresponding to ``addr``.
 */
template <int word_dwidth, typename AddrT>
inline int byte_addr_to_word_index(AddrT addr) {
#pragma HLS INLINE
    return static_cast<int>(addr / (word_dwidth / 8));
}

}  // namespace memmgr
}  // namespace waveflow

#endif // WAVEFLOW_BUILD_MEMMGR_HPP