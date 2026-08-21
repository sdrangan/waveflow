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
// THE THREE CONTRACTS THIS FILE OWES PYTHON
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
// Nothing here hand-rolls a layout it invented: the gate compares against what Python emits, so a
// disagreement is a failure rather than a silent divergence at the degenerate widths where
// hand-rolled packing usually breaks.
#include <cmath>
#include <cstdint>

namespace wfbfm {

/// The converter's sample format.  `nbits` EFFECTIVE bits per sample, `nbits_pack` the container
/// slot it occupies, `justify_shift` where inside that slot the effective bits sit,
/// `samp_per_word` samples per AXIS beat, `full_scale` the amplitude that maps to +1.0 before
/// quantization.
///
/// Aggregate-initialized from Python (`Rfdc._fmt_literal`), so **field order is the literal's
/// order** and the two must not drift.  `nbits_pack` and `justify_shift` are appended rather than
/// interleaved for exactly that reason.
struct RfdcFormat {
    int    nbits         = 16;      ///< EFFECTIVE -- what the converter resolves.  The quantizer.
    int    samp_per_word = 4;
    double full_scale    = 1.0;
    int    nbits_pack    = 16;      ///< CONTAINER -- the slot on the bus.  >= nbits.
    int    justify_shift = 0;       ///< nbits_pack - nbits when MSB-aligned, else 0.

    /// AXIS word width in bits -- the CONTAINER decides the bus.  Real samples only for now;
    /// interleaved I/Q doubles this and is stage 2/4 work (the Python side refuses iq_mode=1 rather
    /// than half-implementing it).
    int word_bits() const { return nbits_pack * samp_per_word; }

    /// Stored-integer range of one sample: [-2^(nbits-1), 2^(nbits-1)-1].  EFFECTIVE bits.
    int64_t q_min() const { return -(int64_t(1) << (nbits - 1)); }
    int64_t q_max() const { return  (int64_t(1) << (nbits - 1)) - 1; }
    /// 2^(nbits-1): the scale that puts full_scale at +1.0.
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

/// Pack `samp_per_word` stored integers into one AXIS word, oldest in the least significant slot,
/// each justified inside its `nbits_pack` container.
inline uint64_t rfdc_pack_word(const int64_t* stored, const RfdcFormat& f) {
    const int w_pack = f.nbits_pack;
    const uint64_t mask = (w_pack >= 64) ? ~uint64_t(0) : ((uint64_t(1) << w_pack) - 1);
    uint64_t w = 0;
    for (int k = 0; k < f.samp_per_word; ++k) {
        // Two's complement in nbits_pack: masking the shifted int64 gives exactly the low bits.
        const uint64_t slot = (uint64_t(stored[k] << f.justify_shift)) & mask;
        w |= slot << (k * w_pack);
    }
    return w;
}

/// Unpack one AXIS word into `samp_per_word` stored integers: sign-extend each container slot, then
/// undo the justification with an ARITHMETIC shift so a negative sample survives.
inline void rfdc_unpack_word(uint64_t word, const RfdcFormat& f, int64_t* stored) {
    const int w_pack = f.nbits_pack;
    const uint64_t mask = (w_pack >= 64) ? ~uint64_t(0) : ((uint64_t(1) << w_pack) - 1);
    const uint64_t sign = uint64_t(1) << (w_pack - 1);
    for (int k = 0; k < f.samp_per_word; ++k) {
        uint64_t slot = (word >> (k * w_pack)) & mask;
        // Sign-extend: a slot at or above the sign bit is negative, so subtract 2^nbits_pack.
        const int64_t v = (slot & sign) ? (int64_t)(slot | ~mask) : (int64_t)slot;
        stored[k] = v >> f.justify_shift;      // arithmetic on a signed type; the low bits are gone
    }
}

/// Real samples -> packed AXIS words.  `n` must be a multiple of `samp_per_word`; the caller sizes
/// `words` at `n / samp_per_word`.
inline void rfdc_pack(const double* samples, int n, const RfdcFormat& f, uint64_t* words) {
    int64_t slot[64];
    const int spw = f.samp_per_word;
    for (int i = 0, w = 0; i < n; i += spw, ++w) {
        for (int k = 0; k < spw; ++k) slot[k] = rfdc_quantize(samples[i + k], f);
        words[w] = rfdc_pack_word(slot, f);
    }
}

/// Packed AXIS words -> real samples.  The exact inverse of `rfdc_pack` on the quantization grid.
inline void rfdc_unpack(const uint64_t* words, int nwords, const RfdcFormat& f, double* samples) {
    int64_t slot[64];
    const int spw = f.samp_per_word;
    for (int w = 0, i = 0; w < nwords; ++w, i += spw) {
        rfdc_unpack_word(words[w], f, slot);
        for (int k = 0; k < spw; ++k) samples[i + k] = rfdc_dequantize(slot[k], f);
    }
}

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RFDC_SAMP_H
