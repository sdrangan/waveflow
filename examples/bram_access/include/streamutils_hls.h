#ifndef STREAMUTILS_HLS_H
#define STREAMUTILS_HLS_H

#include <ap_int.h>
#include <ap_fixed.h>
#include <cstdint>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif

namespace streamutils {

    template<int W>
    using axi4s_word = ap_axis<W, 0, 0, 0>;

    // framed_word — the INTERNAL packet-carrying word (plans/memstream_inband.md).  Vitis rejects
    // ap_axis on an internal hls::stream FIFO (HLS 214-208: reserved for interface ports), so an
    // internal channel that must carry a packet boundary uses this plain struct instead.  Its members
    // are named to MATCH ap_axis (.data / .last) so one templated helper serves both word types; it is
    // a strict subset of ap_axis, minus the AXI-only keep/strb/user/id/dest.
    template<int W>
    struct framed_word {
        ap_uint<W>   data;
        ap_uint<1>   last;
    };

    // set_axis_aux — the ONLY difference between an axi4s beat and a framed beat on the write side:
    // ap_axis carries keep/strb side-channels, framed_word does not.  Overload-resolved at compile
    // time, so each instantiation of write_boundary_word below is separate, branch-free code.
    template<int W> inline void set_axis_aux(framed_word<W>&)   {}                       // internal: none
    template<int W> inline void set_axis_aux(axi4s_word<W>& w)  { w.keep = -1; w.strb = -1; }

    // write_boundary_word / read_boundary_word — one realization for BOTH boundary-carrying word types
    // (axi4s_word at a top-level port, framed_word on an internal FIFO), templated on the word type.
    // The body only touches .data / .last, which both expose; the write's keep/strb difference is
    // isolated in set_axis_aux.  A template is not a runtime conditional: WordT=axi4s_word and
    // WordT=framed_word compile to two separate, fully-optimized functions.
    template<typename WordT, int W>
    inline void write_boundary_word(hls::stream<WordT>& s, ap_uint<W> data, bool last) {
        WordT w;
        w.data = data;
        w.last = last;
        set_axis_aux(w);
        s.write(w);
    }

    template<typename WordT, int W>
    inline ap_uint<W> read_boundary_word(hls::stream<WordT>& s, bool& last) {
        WordT w = s.read();
        last = (bool)w.last;
        return w.data;
    }

    enum class tlast_status {
        no_tlast,
        tlast_at_end,
        tlast_early,
    };

    struct tlast_status_info {
        static constexpr int count = 3;
        static const char* names[count];
    };

    /**
     * Reinterprets the 32 bits of a float as an unsigned integer
     * without performing any type truncation or rounding.
     */
    inline uint32_t float_to_uint(float f) {
        union {
            float f_val;
            uint32_t u_val;
        } converter;
        converter.f_val = f;
        return converter.u_val;
    }

    /**
     * Reinterprets a 32-bit unsigned integer as a float.
     * Critical for restoring floating point data from a bitstream.
     */
    inline float uint_to_float(uint32_t u) {
        union {
            uint32_t u_val;
            float f_val;
        } converter;
        converter.u_val = u;
        return converter.f_val;
    }

    /**
     * Reinterprets the 64 bits of a double as an unsigned integer
     * (bit pattern, no value conversion) -- the double analogue of float_to_uint.
     */
    inline uint64_t double_to_uint(double d) {
        union {
            double d_val;
            uint64_t u_val;
        } converter;
        converter.d_val = d;
        return converter.u_val;
    }

    /**
     * Reinterprets a 64-bit unsigned integer as a double.
     */
    inline double uint_to_double(uint64_t u) {
        union {
            uint64_t u_val;
            double d_val;
        } converter;
        converter.u_val = u;
        return converter.d_val;
    }

    /**
     * Reinterprets the raw bits of an ap_fixed/ap_ufixed value as an unsigned
     * integer of the same width (the .range() bit pattern) — a bit-reinterpret,
     * NOT a value conversion. Templated on the fixed-point type T (uses T::width),
     * so one helper serves any ap_fixed<W,I,Q,O> / ap_ufixed<...>.
     */
    template<typename T>
    inline ap_uint<T::width> fixed_to_bits(T x) {
        return x.range(T::width - 1, 0);
    }

    /**
     * Inverse of fixed_to_bits: builds an ap_fixed/ap_ufixed value from its raw
     * W-bit pattern (bit-reinterpret, not value conversion). This is what restores
     * a fixed-point value from a bitstream word.
     */
    template<typename T>
    inline T bits_to_fixed(ap_uint<T::width> bits) {
        T x;
        x.range(T::width - 1, 0) = bits;
        return x;
    }

    /**
     * Helper to write a word to an AXI4-Stream with TLAST support.
     * Sets TKEEP and TSTRB to all-ones by default.
     */
    template<int W>
    void write_axi4_word(hls::stream<axi4s_word<W>> &s, ap_uint<W> data, bool tlast) {
        axi4s_word<W> pkt;
        pkt.data = data;
        pkt.last = tlast;
        pkt.keep = -1; // -1 in ap_uint sets all bits to 1
        pkt.strb = -1;
        s.write(pkt);
    }

    /**
     * Reads and discards AXI4-Stream words until a TLAST-terminated word is seen.
     * Useful for resynchronizing to the next packet boundary after a framing error.
     */
    template<int W>
    void flush_axi4_stream_to_tlast(hls::stream<axi4s_word<W>> &s) {
        bool done = false;
        while (!done) {
#pragma HLS PIPELINE II=1
            axi4s_word<W> pkt = s.read();
            done = pkt.last;
        }
    }

} // namespace streamutils

#endif // STREAMUTILS_HLS_H