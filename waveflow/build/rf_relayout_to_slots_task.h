#ifndef WAVEFLOW_RF_RELAYOUT_TO_SLOTS_TASK_H
#define WAVEFLOW_RF_RELAYOUT_TO_SLOTS_TASK_H
// rf_relayout_to_slots_task.h — one DENSELY-PACKED word in, one converter word out.
//
// The inverse of rf_relayout_to_dense_task.h, and the direction a TX path takes: logic (or a host
// arena) holds densely-packed effective-width samples, and the converter wants them justified inside
// its container slots.
//
// Exact in this direction unconditionally.  Going out to the wider slot discards nothing, so
// to_dense(to_slots(d)) == d always; the other order is exact only when each slot's low SHIFT bits
// are zero, which is what a left-justified converter guarantees and what a hand-built test vector
// has to respect.  See waveflow/hw/rf_relayout.py § "Round trips, and which one is exact".
//
// As next door: the word<->slot step is the GENERATED serializer's, the shift is this body's, and
// nothing here writes a `.range()`.
#include "hls_stream.h"
#include <ap_int.h>

#include "rf_dense_elem_array_utils.h"
#include "rf_slot_elem_array_utils.h"

/// @tparam W      word width in bits -- the same on both ports.
/// @tparam NSLOT  slots (and dense samples) in one word.
/// @tparam SHIFT  bits the effective sample is shifted UP by to sit in its container slot.
template <int W, int NSLOT, int SHIFT>
static void rf_relayout_to_slots_task(hls::stream<ap_uint<W> >& s_in,
                                      hls::stream<ap_uint<W> >& s_out) {
    while (1) {
#pragma HLS PIPELINE II=1
        ap_uint<W> in = s_in.read();

        rf_dense_elem_array_utils::value_type dense[NSLOT];
#pragma HLS ARRAY_PARTITION variable=dense complete
        rf_dense_elem_array_utils::read_array_lane<W>(&in, dense, NSLOT);

        rf_slot_elem_array_utils::value_type slot[NSLOT];
#pragma HLS ARRAY_PARTITION variable=slot complete
        for (int k = 0; k < NSLOT; k++) {
#pragma HLS UNROLL
            // Widen FIRST, then shift.  The other order would shift inside the narrow dense type and
            // drop the top SHIFT bits of every sample -- a full-scale sample would come out small,
            // which is a signal-level error rather than a crash and would pass a ramp that never
            // reaches full scale.
            slot[k] = ((rf_slot_elem_array_utils::value_type)dense[k]) << SHIFT;
        }

        ap_uint<W> out = 0;
        rf_slot_elem_array_utils::write_array_lane<W>(slot, &out, NSLOT);
        s_out.write(out);
    }
}

#endif  // WAVEFLOW_RF_RELAYOUT_TO_SLOTS_TASK_H
