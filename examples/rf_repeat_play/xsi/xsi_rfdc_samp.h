#ifndef WAVEFLOW_XSI_RFDC_SAMP_H
#define WAVEFLOW_XSI_RFDC_SAMP_H
// xsi_rfdc_samp.h — the converter's SAMPLE domain: quantize, dequantize, pack, unpack.
//
// Split out of the RFDC models for the same reason XsiSimObj was split out of xsi_bfm.h: everything
// here is arithmetic, none of it needs a simulator, and a bit-exactness claim that could only be
// tested inside a full Vivado run would in practice not be tested at all.  tests/build/
// test_xsi_rfdc_samp.py compiles this with a plain g++ and checks it against Python's FixedField and
// the schema serializer -- a conformance twin, not a re-reading of the spec.
//
// EFFECTIVE BITS AND CONTAINER BITS ARE TWO NUMBERS
//
// `nbits` is what the converter RESOLVES; `nbits_pack` is the width of the slot it rides in.  They
// are equal on a part whose resolution happens to match its slot width and different on one whose
// does not -- a ZU48DR resolves 14 bits into a 16-bit AXI-Stream slot.  The Python twin states both
// on `RfdcSampWord`, and `RfdcFormat` is the literal it emits.  One number meaning both is a defect
// with no symptom: match it to the bus and the model's quantization is finer than the hardware's.
//
// THE FOUR CONTRACTS THIS FILE OWES PYTHON
//
// 1. Quantization is `ap_fixed<nbits,1>` with AP_RND + AP_SAT, which waveflow implements as integer
//    math in waveflow/utils/fixputils.py:
//        stored = floor(x/full_scale * 2^(nbits-1) + 0.5),  clamped to [-2^(nbits-1), 2^(nbits-1)-1]
//    AP_RND is round-half-UP (toward +inf), not round-half-even and not round-half-away: -0.5 stored
//    units rounds to 0, not to -1.  AP_SAT clamps; a converter clips, it does not wrap.
//    NOTE the width: `nbits`, the EFFECTIVE count.  Never `nbits_pack`.
//
// 2. Justification.  A stored value is placed in its slot shifted left by `justify_shift`, which the
//    Python side computes from `RfdcSampWord.justify` -- `nbits_pack - nbits` when the effective
//    bits are MSB-aligned in the container, 0 when they are LSB-aligned.  **The Python default is an
//    UNCONFIRMED assumption** (a PG269 question, to be settled in the lab); this file does not
//    choose, it is told.  Zero whenever the two widths agree, which is every format that predates
//    the split.
//
// 3. Packing is time-ascending from the LSBs, each sample in a fixed `nbits_pack` slot, two's
//    complement -- the layout PG269 specifies and plans/circ_buf_fac.md works through.  Verified
//    against the schema serializer rather than asserted: samples [0, 64, -64, -128] in 8-bit slots
//    pack to 0x80c04000, i.e. the OLDEST sample in the LEAST significant slot.
//
// 4. I/Q slot order.  With `iq_mode`, a complex sample occupies TWO adjacent slots and `iq_order`
//    says which of I and Q takes the lower one.  Everything else is unchanged -- the quantizer, the
//    justification and the word layout do not learn about complex-ness; only which component lands
//    in which slot does, and that is one swap (`rfdc_iq_swap`).  **The Python default is `i_low` and
//    the bring-up log has evidence against it**, so this file is told rather than choosing, exactly
//    as it is for justification.
//
// Nothing here hand-rolls a layout it invented: the gate compares against what Python emits, so a
// disagreement is a failure rather than a silent divergence at the degenerate widths where
// hand-rolled packing usually breaks.
#include <cmath>
#include <cstdint>

namespace wfbfm {

/// `iq_mode`: is one sample real, or a complex (I, Q) pair occupying two adjacent slots?
static const int RFDC_REAL = 0;
static const int RFDC_IQ   = 1;

/// `iq_order`: which of I and Q takes the **lower** (earlier, less significant) slot of each pair.
///
/// **The Python default is `i_low` and the board bring-up log has evidence against it** — the
/// quad-tile layout is quoted as `{I1, Q1, I0, Q0}`, which read in the same convention as the real
/// case puts Q in the lower slot.  That is an inference from a community source, not a measurement,
/// so nothing here chooses: the value is read off `RfdcFormat` on every path, and a lab correction
/// stays the one-field change it already is on the Python side.
static const int RFDC_I_LOW = 0;
static const int RFDC_Q_LOW = 1;

/// The converter's sample format.  `nbits` EFFECTIVE bits per sample, `nbits_pack` the container
/// slot it occupies, `justify_shift` where inside that slot the effective bits sit,
/// `samp_per_word` samples per AXIS beat, `full_scale` the amplitude that maps to +1.0 before
/// quantization, and the two I/Q rules.
///
/// Aggregate-initialized from Python (`Rfdc._fmt_literal`), so **field order is the literal's
/// order** and the two must not drift.  Every field after `full_scale` is APPENDED rather than
/// grouped with the ones it belongs beside — `nbits_pack` beside `nbits`, `iq_order` beside
/// `samp_per_word` — because a positional literal cannot survive an insertion in the middle: every
/// value after it lands in the wrong member, and `nbits_pack` becoming `samp_per_word` does not
/// even fail to compile.  `test_the_format_literal_rfdc_emits_reads_back_field_for_field` is what
/// holds the order.
struct RfdcFormat {
    int    nbits         = 16;      ///< EFFECTIVE -- what the converter resolves.  The quantizer.
    int    samp_per_word = 4;       ///< SAMPLES per beat -- complex ones when iq_mode is set.
    double full_scale    = 1.0;
    int    nbits_pack    = 16;      ///< CONTAINER -- the slot on the bus.  >= nbits.
    int    justify_shift = 0;       ///< nbits_pack - nbits when MSB-aligned, else 0.
    int    iq_mode       = RFDC_REAL;   ///< RFDC_REAL | RFDC_IQ
    int    iq_order      = RFDC_I_LOW;  ///< RFDC_I_LOW | RFDC_Q_LOW -- see above

    /// Container slots one beat carries: `samp_per_word`, DOUBLED for interleaved I/Q because a
    /// complex sample occupies two of them.
    ///
    /// This is the count every loop below runs to, and that is deliberate: a slot is a slot whether
    /// it holds I, Q or a real sample, so the packing arithmetic never learns about complex-ness.
    /// What I/Q changes is *which component* lands in which slot, which is `iq_order`'s job alone.
    int slots_per_word() const { return samp_per_word * (iq_mode == RFDC_IQ ? 2 : 1); }

    /// AXIS word width in bits -- the CONTAINER decides the bus.  An I/Q design fits the same bus by
    /// HALVING `samp_per_word`, which is the arithmetic the Python word type already does.
    int word_bits() const { return nbits_pack * slots_per_word(); }

    /// Stored-integer range of one sample: [-2^(nbits-1), 2^(nbits-1)-1].  EFFECTIVE bits.
    int64_t q_min() const { return -(int64_t(1) << (nbits - 1)); }
    int64_t q_max() const { return  (int64_t(1) << (nbits - 1)) - 1; }
    /// 2^(nbits-1): the scale that puts full_scale at +1.0.  I and Q are quantized identically --
    /// they are two real values of the same converter, not a complex type with its own rule.
    double  q_scale() const { return double(int64_t(1) << (nbits - 1)); }
};

/// One real sample -> its stored integer.  AP_RND (round half up) then AP_SAT (clamp).
inline int64_t rfdc_quantize(double x, const RfdcFormat& f) {
    const double scaled = (x / f.full_scale) * f.q_scale();
    // floor(v + 0.5) is round-half-UP for negatives too -- floor(-0.5) == -1 but floor(-0.5+0.5) == 0,
    // which is what AP_RND does and what fixputils.quantize_real computes.  std::llround would round
    // half AWAY from zero and disagree on exactly the ties.
    const double q = std::floor(scaled + 0.5);
    if (q <= double(f.q_min())) return f.q_min();      // AP_SAT: clip, never wrap
    if (q >= double(f.q_max())) return f.q_max();
    return (int64_t)q;
}

/// One stored integer -> its real value.  Exact: a power-of-two scale.
inline double rfdc_dequantize(int64_t q, const RfdcFormat& f) {
    return (double(q) / f.q_scale()) * f.full_scale;
}

/// The slot buffer size the two block functions below use.
///
/// Named rather than left as a bare `[64]` so the bound has a reason beside it.  The **true**
/// maximum is 32: a converter port's word is capped at 64 bits (`Rfdc` refuses wider) and a
/// container slot is at least 2 bits, so a beat cannot hold more than 32 slots — interleaved I/Q
/// included, since it spends its doubling on halving `samp_per_word`.  The value is left at 64
/// anyway, which is what it was before I/Q existed: the slack costs nothing and shrinking a buffer
/// is not a change worth making on an argument rather than a measurement.
static const int RFDC_MAX_SLOTS = 64;

/// Pack `slots_per_word()` slot values into one AXIS word, oldest in the least significant slot,
/// each justified inside its `nbits_pack` container.
///
/// **Slots, not samples.**  For real data the two counts are the same; for interleaved I/Q a word
/// holds twice as many slots as samples, and the caller has already put I and Q in the order
/// `iq_order` asks for.  Keeping this function ignorant of complex-ness is what stops the layout
/// rule and the component-order rule from being two half-implementations of one thing.
inline uint64_t rfdc_pack_word(const int64_t* slots, const RfdcFormat& f) {
    const int w_pack = f.nbits_pack;
    const uint64_t mask = (w_pack >= 64) ? ~uint64_t(0) : ((uint64_t(1) << w_pack) - 1);
    const int n_slot = f.slots_per_word();
    uint64_t w = 0;
    for (int k = 0; k < n_slot; ++k) {
        // Two's complement in nbits_pack: masking the shifted int64 gives exactly the low bits.
        const uint64_t slot = (uint64_t(slots[k] << f.justify_shift)) & mask;
        w |= slot << (k * w_pack);
    }
    return w;
}

/// Unpack one AXIS word into `slots_per_word()` slot values: sign-extend each container slot, then
/// undo the justification with an ARITHMETIC shift so a negative sample survives.
inline void rfdc_unpack_word(uint64_t word, const RfdcFormat& f, int64_t* slots) {
    const int w_pack = f.nbits_pack;
    const uint64_t mask = (w_pack >= 64) ? ~uint64_t(0) : ((uint64_t(1) << w_pack) - 1);
    const uint64_t sign = uint64_t(1) << (w_pack - 1);
    const int n_slot = f.slots_per_word();
    for (int k = 0; k < n_slot; ++k) {
        uint64_t slot = (word >> (k * w_pack)) & mask;
        // Sign-extend: a slot at or above the sign bit is negative, so subtract 2^nbits_pack.
        const int64_t v = (slot & sign) ? (int64_t)(slot | ~mask) : (int64_t)slot;
        slots[k] = v >> f.justify_shift;       // arithmetic on a signed type; the low bits are gone
    }
}

/// Swap each (lower, upper) slot pair in place — the whole of what `iq_order` means.
///
/// It is its **own** involution rather than two functions, because interleaving and de-interleaving a
/// pair are the same operation: a swap undone by a swap.  `RFDC_I_LOW` is the identity, so this is a
/// no-op on the declared default and on every real format.
///
/// The layout it produces at `q_low`, for `samp_per_word = 2`, reading a word from its MSBs down:
/// `{I1, Q1, I0, Q0}` — which is exactly how the quad-tile RFDC's bus is quoted, and the reason the
/// bring-up log lists `iq_order` above `justify`.
inline void rfdc_iq_swap(int64_t* slots, const RfdcFormat& f) {
    if (f.iq_mode != RFDC_IQ || f.iq_order != RFDC_Q_LOW) return;
    const int n_slot = f.slots_per_word();
    for (int k = 0; k + 1 < n_slot; k += 2) {
        const int64_t t = slots[k];
        slots[k] = slots[k + 1];
        slots[k + 1] = t;
    }
}

/// Sample COMPONENTS -> packed AXIS words.
///
/// *n* counts **doubles**, which is the same as samples for real data and twice the samples for
/// I/Q — where the components arrive `(re, im)` adjacent, one pair per complex sample.  That is not
/// a new convention: it is the layout the RF bundle already stores complex blocks in
/// (`waveflow/simulation/rf_tb.py`) and the layout `RfBlockMsg::data` therefore carries, so no
/// caller transposes anything.
///
/// `n` must be a multiple of `slots_per_word()`; the caller sizes `words` at
/// `n / slots_per_word()`.  One expression for both modes, because a slot is a slot.
inline void rfdc_pack(const double* samples, int n, const RfdcFormat& f, uint64_t* words) {
    int64_t slot[RFDC_MAX_SLOTS];
    const int n_slot = f.slots_per_word();
    for (int i = 0, w = 0; i < n; i += n_slot, ++w) {
        // Quantize FIRST, in arrival order, then reorder: I and Q are two real values of the same
        // converter, so the quantizer never needs to know which is which.
        for (int k = 0; k < n_slot; ++k) slot[k] = rfdc_quantize(samples[i + k], f);
        rfdc_iq_swap(slot, f);
        words[w] = rfdc_pack_word(slot, f);
    }
}

/// Packed AXIS words -> sample components.  The exact inverse of `rfdc_pack` on the quantization
/// grid, and it writes `nwords * slots_per_word()` doubles in the same `(re, im)`-adjacent order.
inline void rfdc_unpack(const uint64_t* words, int nwords, const RfdcFormat& f, double* samples) {
    int64_t slot[RFDC_MAX_SLOTS];
    const int n_slot = f.slots_per_word();
    for (int w = 0, i = 0; w < nwords; ++w, i += n_slot) {
        rfdc_unpack_word(words[w], f, slot);
        rfdc_iq_swap(slot, f);                 // its own inverse: a swap undone by a swap
        for (int k = 0; k < n_slot; ++k) samples[i + k] = rfdc_dequantize(slot[k], f);
    }
}

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RFDC_SAMP_H
