// poll_until_impl.tpp
// Reusable C++ ring-poll primitive — the synthesizable twin of
// waveflow.hw.memif.MMIFMaster.poll_until (step 5 of the poll_until LT model,
// plans/poll_until_lt_model.md).
//
// The extractor lowers `val = master.poll_until(addr, cond, poll_interval)` to a
// call to one of these (waveflow/build/hwgen.py::_emit_poll_until), with the
// restricted PollCond (== / !=, rhs a constant or a runtime-read local) chosen at
// codegen time.  The ring-dequeue hook (aximm_queue_impl::queue_get) is expressed
// in terms of the same primitive — its `while (head == tail)` non-empty wait is
// `poll_until_ne(gmem, tail_word, head)` — so there is one poll loop, not two.
//
// `poll_interval` / `poll_beat_cost` / `discovery` are loosely-timed (AT-model)
// concerns with no hardware meaning; they never reach this code (a real poll just
// spins on the memory word).

#ifndef WAVEFLOW_BUILD_POLL_UNTIL_IMPL_TPP
#define WAVEFLOW_BUILD_POLL_UNTIL_IMPL_TPP

#include <ap_int.h>

namespace poll_until_impl {

// Poll word `word_idx` of `gmem` until (value == rhs); return the satisfying value.
//   MEM_BW   : memory data width in bits (the gmem word width).
template <int MEM_BW>
ap_uint<MEM_BW> poll_until_eq(ap_uint<MEM_BW>* gmem, int word_idx, ap_uint<MEM_BW> rhs) {
    ap_uint<MEM_BW> v = gmem[word_idx];
poll_until_eq_loop:
    while (!(v == rhs)) {
        v = gmem[word_idx];
    }
    return v;
}

// Poll word `word_idx` of `gmem` until (value != rhs); return the satisfying value.
// This is the ring-dequeue's non-empty wait: poll `tail` until `tail != head`.
template <int MEM_BW>
ap_uint<MEM_BW> poll_until_ne(ap_uint<MEM_BW>* gmem, int word_idx, ap_uint<MEM_BW> rhs) {
    ap_uint<MEM_BW> v = gmem[word_idx];
poll_until_ne_loop:
    while (!(v != rhs)) {
        v = gmem[word_idx];
    }
    return v;
}

}  // namespace poll_until_impl

#endif  // WAVEFLOW_BUILD_POLL_UNTIL_IMPL_TPP
