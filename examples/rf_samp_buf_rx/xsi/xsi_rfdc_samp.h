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
// THE TWO CONTRACTS THIS FILE OWES PYTHON
//
// 1. Quantization is `ap_fixed<nbits,1>` with AP_RND + AP_SAT, which waveflow implements as integer
//    math in waveflow/utils/fixputils.py:
//        stored = floor(x/full_scale * 2^(nbits-1) + 0.5),  clamped to [-2^(nbits-1), 2^(nbits-1)-1]
//    AP_RND is round-half-UP (toward +inf), not round-half-even and not round-half-away: -0.5 stored
//    units rounds to 0, not to -1.  AP_SAT clamps; a converter clips, it does not wrap.
//
// 2. Packing is time-ascending from the LSBs, each sample in a fixed nbits slot, two's complement --
//    the layout PG269 specifies and plans/circ_buf_fac.md works through.  Verified against the
//    schema serializer rather than asserted: samples [0, 64, -64, -128] at nbits=8 pack to
//    0x80c04000, i.e. the OLDEST sample in the LEAST significant slot.
//
// Nothing here hand-rolls a layout it invented: the gate compares against what Python emits, so a
// disagreement is a failure rather than a silent divergence at the degenerate widths where
// hand-rolled packing usually breaks.
#include <cmath>
#include <cstdint>

namespace wfbfm {

/// The converter's sample format.  `nbits` bits per sample, `samp_per_word` samples per AXIS beat,
/// `full_scale` the amplitude that maps to +1.0 before quantization.
struct RfdcFormat {
    int    nbits         = 16;
    int    samp_per_word = 4;
    double full_scale    = 1.0;

    /// AXIS word width in bits.  Real samples only for now; interleaved I/Q doubles this and is
    /// stage 2/4 work (the Python side refuses iq_mode=1 rather than half-implementing it).
    int word_bits() const { return nbits * samp_per_word; }

    /// Stored-integer range of one sample: [-2^(nbits-1), 2^(nbits-1)-1].
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

/// Pack `samp_per_word` stored integers into one AXIS word, oldest in the least significant slot.
inline uint64_t rfdc_pack_word(const int64_t* stored, const RfdcFormat& f) {
    const uint64_t mask = (f.nbits >= 64) ? ~uint64_t(0) : ((uint64_t(1) << f.nbits) - 1);
    uint64_t w = 0;
    for (int k = 0; k < f.samp_per_word; ++k) {
        // Two's complement in nbits: masking a negative int64 gives exactly the low nbits.
        w |= (uint64_t(stored[k]) & mask) << (k * f.nbits);
    }
    return w;
}

/// Unpack one AXIS word into `samp_per_word` stored integers, sign-extending each slot.
inline void rfdc_unpack_word(uint64_t word, const RfdcFormat& f, int64_t* stored) {
    const uint64_t mask = (f.nbits >= 64) ? ~uint64_t(0) : ((uint64_t(1) << f.nbits) - 1);
    const uint64_t sign = uint64_t(1) << (f.nbits - 1);
    for (int k = 0; k < f.samp_per_word; ++k) {
        uint64_t slot = (word >> (k * f.nbits)) & mask;
        // Sign-extend: a slot at or above the sign bit is negative, so subtract 2^nbits.
        stored[k] = (slot & sign) ? (int64_t)(slot | ~mask) : (int64_t)slot;
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
