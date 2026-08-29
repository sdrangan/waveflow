#ifndef BRAM_ACCESS_BRAM_CMD_RANGE_H
#define BRAM_ACCESS_BRAM_CMD_RANGE_H
// bram_cmd_range.h — the one arithmetic check both task bodies share.
//
// This file used to define the status codes as well (BRAM_CMD_ST_OK / BRAM_CMD_ST_OUT_OF_RANGE).
// They are gone: the status is now a real `enum class BramStatus`, GENERATED from the Python
// `BramStatus` IntEnum into bram_status.h, so the two languages cannot spell it differently.  What
// is left here is a range test, which is a piece of the design's LOGIC rather than a piece of its
// message layout -- and layout is the thing that must have exactly one author.
#include <ap_int.h>

/// Is `[p, p + n)` inside a memory of `N` words?
///
/// Written as `n <= N && p <= N - n` rather than `p + n <= N` **on purpose**: the operands are
/// unsigned and a sum of two legal-looking values wraps, which would turn an out-of-range command
/// into an accepted one at exactly the widths where the memory is most likely to be full.  Neither
/// term here can overflow.
template <int W, int N>
static inline bool bram_cmd_in_range(ap_uint<W> p, ap_uint<W> n) {
#pragma HLS INLINE
    return (n <= (ap_uint<W>)N) && (p <= (ap_uint<W>)(N - n));
}

#endif  // BRAM_ACCESS_BRAM_CMD_RANGE_H
