#ifndef BRAM_SIMPLE_BRAM_CMD_STATUS_H
#define BRAM_SIMPLE_BRAM_CMD_STATUS_H
// bram_cmd_status.h — the response codes, in the one place both tasks read them from.
//
// TWO CODES, AND NO MORE.  A range that leaves the memory is refused; everything else is OK.  A
// third code -- a legal range whose payload arrives short -- is worth having once a scenario needs
// it, and is deliberately absent until then: an unexercised branch in a teaching example is a
// branch a reader has to take on trust.
//
// Their Python twins are `ST_OK` / `ST_OUT_OF_RANGE` in bram_simple.py, and
// tests/examples/test_bram_simple.py checks the two spellings against each other.  A status code
// that means one thing in Python and another in C++ is a divergence no run would report: both
// backends would answer, both would be checked, and the numbers would simply disagree about which
// answer is which.
#include <ap_int.h>

#define BRAM_CMD_ST_OK 0
#define BRAM_CMD_ST_OUT_OF_RANGE 1

/// Is `[p, p + n)` inside a memory of `N` words?
///
/// Written as `n <= N && p <= N - n` rather than `p + n <= N` **on purpose**: the operands are
/// `ap_uint<W>` and at W = 16 with N = 1024 the sum of two legal-looking values wraps, which would
/// turn an out-of-range command into an accepted one at exactly the widths where the memory is most
/// likely to be full.  Neither term here can overflow.
template <int W, int N>
static inline bool bram_cmd_in_range(ap_uint<W> p, ap_uint<W> n) {
#pragma HLS INLINE
    return (n <= (ap_uint<W>)N) && (p <= (ap_uint<W>)(N - n));
}

#endif  // BRAM_SIMPLE_BRAM_CMD_STATUS_H
