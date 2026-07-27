// state_accum_accumulate_impl.cpp — the HAND-WRITTEN body of the `accumulate` hook.
//
// The generated task body (include/state_accum_task.h) declares this and calls it; nothing
// lowers it from the Python.  Its Python twin is StateAccum.accumulate, and the two are kept
// in agreement by the gate, not by the compiler.
//
// `total` is the add_state array: it arrives as a plain pointer (an array argument decays), so
// writes through it land in the caller's `static` and survive to the next firing.  That
// survival is exactly what the XSI gate exists to demonstrate.
#include "state_accum_task.h"

namespace state_accum_impl {

Vec4 accumulate(Vec4 x, ap_uint<32> total[4]) {
#pragma HLS INLINE
    Vec4 y;
ACC: for (int i = 0; i < 4; ++i) {
#pragma HLS UNROLL
        total[i] = total[i] + x.data[i];
        y.data[i] = total[i];
    }
    return y;
}

}  // namespace state_accum_impl
