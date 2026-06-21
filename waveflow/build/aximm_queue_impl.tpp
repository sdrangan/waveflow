// aximm_queue_impl.tpp
// Hand-written C++ ring-dequeue hook for AXIMMQueue.get (decision D5, Strategy A).
//
// This is the C++ twin of the synthesizable Python AXIMMQueue.get typed single-element
// path (waveflow/hw/aximm_queue.py); the extractor lowers `cmd = self.cmd_queue.get(self.Cmd)`
// to a call to `aximm_queue_impl::queue_get<CmdT, MEM_BW, BASE, CAP, EW>(gmem, cmd)`
// (waveflow/build/hwgen.py::_emit_aximm_queue_get).  Mirrors the vmac_compute_impl.tpp hook
// pattern: the synthesizable top includes this header and calls the templated function; the
// ring geometry is fixed by the Python AXIMMQueueLayout and passed as template params.
//
// Ring layout (mirror AXIMMQueueLayout, byte-addressed AXI-MM; one control field per word):
//
//     word 0 : head      (consumer-owned slot index)
//     word 1 : tail      (producer-owned slot index)
//     word 2 : capacity  (informational)
//     word 3 : reserved
//     data   : CAP slots x EW words   (slot i at data_base + i*EW words)
//
// SPSC dequeue (decision 2 — read data BEFORE advancing head, so the producer can never
// reclaim a slot still being read):
//   1) read head; poll tail until head != tail (non-empty);
//   2) read one slot (EW words) at slot(head);
//   3) advance head = (head + 1) % CAP and publish it (one m_axi write);
//   4) deserialize the slot words into the typed command.
//
// `% CAP` is a mask because CAP is a power of two (a static_assert enforces it, like the
// elem_to_word PF check in vmac_compute_impl.tpp — a non-power-of-two would synthesize a
// real hardware divider on the head-advance).

#ifndef WAVEFLOW_BUILD_AXIMM_QUEUE_IMPL_TPP
#define WAVEFLOW_BUILD_AXIMM_QUEUE_IMPL_TPP

#include <ap_int.h>

namespace aximm_queue_impl {

// Number of control words that precede the data slots (mirror
// AXIMMQueueLayout.NUM_CONTROL_WORDS: head, tail, capacity, reserved).
static const int AXIMM_Q_CONTROL_WORDS = 4;

// Blocking single-element ring dequeue over the m_axi `gmem` pointer.
//   CmdT    : the generated command struct (e.g. VmacCmd).
//   MEM_BW  : memory data width in bits (the gmem word width).
//   BASE    : ring base BYTE address (must be word-aligned).
//   CAP     : ring capacity in slots (power of two).
//   EW      : words per slot (== CmdT.nwords_per_inst(MEM_BW)).
template <typename CmdT, int MEM_BW, int BASE, int CAP, int EW>
void queue_get(ap_uint<MEM_BW>* gmem, CmdT& out) {
    static_assert(CAP >= 2 && (CAP & (CAP - 1)) == 0,
                  "AXIMMQueue capacity must be a power of two so the head-advance wrap is a "
                  "mask; a non-power-of-two capacity would synthesize a hardware divider.");
    static_assert(BASE % (MEM_BW / 8) == 0,
                  "AXIMMQueue base address must be word-aligned for the gmem word index map.");

    const int base_word = BASE / (MEM_BW / 8);              // control words start here
    const int data_word = base_word + AXIMM_Q_CONTROL_WORDS; // data slots start here

    // 1) read head once, then poll tail until the ring is non-empty.  head is consumer-owned
    //    (only we write it), so it is stable across the poll; only tail moves (producer).
    const int head = (int)gmem[base_word + 0];
    int tail = (int)gmem[base_word + 1];
poll_nonempty:
    while (head == tail) {
        tail = (int)gmem[base_word + 1];
    }

    // 2) read one slot (EW words) at slot(head) BEFORE advancing head (SPSC ordering crux).
    ap_uint<MEM_BW> slot[EW];
    const int slot_word = data_word + head * EW;
    for (int w = 0; w < EW; ++w) {
#pragma HLS PIPELINE II=1
        slot[w] = gmem[slot_word + w];
    }

    // 3) advance head = (head + 1) % CAP (mask) and publish — only now is the slot reclaimable.
    gmem[base_word + 0] = (ap_uint<MEM_BW>)((head + 1) & (CAP - 1));

    // 4) deserialize the slot words into the typed command via the generated struct's
    //    word-array unpack (the m_axi dual of its stream read; the csim conformance TB uses the
    //    same `read_array<word_bw>` entry point).  The command DataSchemaStep must include
    //    MEM_BW in its word_bw_supported so this instantiation exists.
    out.template read_array<MEM_BW>(slot);  // `template` disambiguator: CmdT is dependent
}

}  // namespace aximm_queue_impl

#endif  // WAVEFLOW_BUILD_AXIMM_QUEUE_IMPL_TPP
