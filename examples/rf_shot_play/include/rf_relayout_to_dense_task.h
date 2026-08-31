#ifndef WAVEFLOW_RF_RELAYOUT_TO_DENSE_TASK_H
#define WAVEFLOW_RF_RELAYOUT_TO_DENSE_TASK_H
// rf_relayout_to_dense_task.h — one converter word in, one DENSELY-PACKED word out.
//
// plans/rf_shot_buf.md § "The logic-side port".  The buffer exposes samples and owns the converter's
// packing, so this is where 14-in-16 stops being visible to anything upstream:
//
//     in:   [ slot3 | slot2 | slot1 | slot0 ]     4 x 16 bits, 14 effective, left-justified
//     out:  [00000000][ s3 ][ s2 ][ s1 ][ s0 ]    4 x 14 bits, 8 idle high bits
//
// NOTHING HERE HAND-ROLLS A `.range()`.
//
// The word<->slot step belongs to the GENERATED serializers (rf_slot_elem_array_utils.h /
// rf_dense_elem_array_utils.h), and the only arithmetic this body owns is the justification SHIFT --
// the one rule a serializer cannot know.  That is the same split rfdc_samp_word.pack() makes on the
// Python side, and it is why the two backends cannot disagree about slot order: neither of them
// decides it.  Hand-rolling the shifts here would put a second, silently-divergent statement of the
// packing contract in the repo, and the bug hides at one slot per word where slot order is
// unobservable.
//
// THE II THIS BODY REACHES IS A MEASUREMENT, NOT A PREDICTION.
//
// When bits_per_samp == bits_per_samp_pack the whole conversion is the identity, which is every
// configuration in this repo except the RFSoC 4x2 preset -- so this path was UNEXERCISED and
// "shift and mask per slot holds II=1" was a guess.  examples/rf_relayout csynths it at 14-in-16 and
// records the achieved PipelineII, and an XSI run checks the bits, because csynth reporting II=1 on
// a body that is wrong at RTL is a thing that has happened here (commit a2f93e0: 0xFFFF for 9984
// samples with every counter green).
#include "hls_stream.h"
#include <ap_int.h>

#include "rf_dense_elem_array_utils.h"
#include "rf_slot_elem_array_utils.h"

/// @tparam W      word width in bits -- the SAME on both ports.  That is the point of taking 64 and
///                not 56: the conversion is a pure re-layout inside one width, so nothing downstream
///                changes width when the converter's resolution does.
/// @tparam NSLOT  slots (and dense samples) in one word; twice samp_per_word for interleaved I/Q.
/// @tparam SHIFT  bits the effective sample sits above the bottom of its container slot
///                (`bits_per_samp_pack - bits_per_samp` when left-justified, else 0).  **0 makes
///                this body the identity**, which is what had to be avoided to measure anything.
template <int W, int NSLOT, int SHIFT>
static void rf_relayout_to_dense_task(hls::stream<ap_uint<W> >& s_in,
                                      hls::stream<ap_uint<W> >& s_out) {
    while (1) {
#pragma HLS PIPELINE II=1
        ap_uint<W> in = s_in.read();

        rf_slot_elem_array_utils::value_type slot[NSLOT];
#pragma HLS ARRAY_PARTITION variable=slot complete
        rf_slot_elem_array_utils::read_array_lane<W>(&in, slot, NSLOT);

        rf_dense_elem_array_utils::value_type dense[NSLOT];
#pragma HLS ARRAY_PARTITION variable=dense complete
        for (int k = 0; k < NSLOT; k++) {
#pragma HLS UNROLL
            // An ARITHMETIC right shift: `value_type` is a signed ap_int, so a negative sample keeps
            // its sign.  The bits below SHIFT are discarded, which is exactly what the hardware does
            // with them -- the converter never set them.
            dense[k] = (rf_dense_elem_array_utils::value_type)(slot[k] >> SHIFT);
        }

        ap_uint<W> out = 0;
        rf_dense_elem_array_utils::write_array_lane<W>(dense, &out, NSLOT);
        s_out.write(out);
    }
}

#endif  // WAVEFLOW_RF_RELAYOUT_TO_DENSE_TASK_H
