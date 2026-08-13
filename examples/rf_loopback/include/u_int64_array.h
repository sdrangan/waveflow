#ifndef INCLUDE_U_INT64_ARRAY_H
#define INCLUDE_U_INT64_ARRAY_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct UInt64Array {
    ap_uint<64> data[64];

    static constexpr int bitwidth = 4096;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 64;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const UInt64Array& data) {
        ap_uint<bitwidth> res = 0;
        int bitpos = 0;
        for (int i0 = 0; i0 < 64; ++i0) {
            res.range(bitpos + 64 - 1, bitpos) = data.data[i0];
            bitpos += 64;
        }
        return res;
    }

    static UInt64Array unpack_from_uint(const ap_uint<bitwidth>& packed) {
        UInt64Array data;
        int bitpos = 0;
        for (int i0 = 0; i0 < 64; ++i0) {
            data.data[i0] = (ap_uint<64>)(packed.range(bitpos + 64 - 1, bitpos));
            bitpos += 64;
        }
        return data;
    }

    template<int word_bw>
    static constexpr int pf() {
        return word_bw / 64;
    }

    template<int word_bw>
    static void read_stream_elem_impl(word_bw_tag<word_bw>, hls::stream<ap_uint<word_bw>>& s, ap_uint<64>* out, int n) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream_elem");
        (void)s;
        (void)out;
        (void)n;
    }

    static void read_stream_elem_impl(word_bw_tag<64>, hls::stream<ap_uint<64>>& s, ap_uint<64>* out, int n) {
        #pragma HLS INLINE
        if (n > 0) {
            ap_uint<64> w = s.read();
            out[0] = (ap_uint<64>)(w);
        }
    }

    template<int word_bw>
    static void read_stream_elem(hls::stream<ap_uint<word_bw>>& s, ap_uint<64> out[pf<word_bw>()], int n = pf<word_bw>()) {
        #pragma HLS INLINE
        read_stream_elem_impl(word_bw_tag<word_bw>{}, s, out, n);
    }

    template<int word_bw>
    static void read_axi4_stream_elem_impl(word_bw_tag<word_bw>, hls::stream<streamutils::axi4s_word<word_bw>>& s, ap_uint<64>* out, int n) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream_elem");
        (void)s;
        (void)out;
        (void)n;
    }

    static void read_axi4_stream_elem_impl(word_bw_tag<64>, hls::stream<streamutils::axi4s_word<64>>& s, ap_uint<64>* out, int n) {
        #pragma HLS INLINE
        if (n > 0) {
            ap_uint<64> w = s.read().data;
            out[0] = (ap_uint<64>)(w);
        }
    }

    template<int word_bw>
    static void read_axi4_stream_elem(hls::stream<streamutils::axi4s_word<word_bw>>& s, ap_uint<64> out[pf<word_bw>()], int n = pf<word_bw>()) {
        #pragma HLS INLINE
        read_axi4_stream_elem_impl(word_bw_tag<word_bw>{}, s, out, n);
    }

    template<int word_bw>
    static void write_stream_elem_impl(word_bw_tag<word_bw>, hls::stream<ap_uint<word_bw>>& s, const ap_uint<64>* in, int n) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream_elem");
        (void)s;
        (void)in;
        (void)n;
    }

    static void write_stream_elem_impl(word_bw_tag<64>, hls::stream<ap_uint<64>>& s, const ap_uint<64>* in, int n) {
        #pragma HLS INLINE
        if (n > 0) {
            ap_uint<64> w = in[0];
            s.write(w);
        }
    }

    template<int word_bw>
    static void write_stream_elem(hls::stream<ap_uint<word_bw>>& s, const ap_uint<64> in[pf<word_bw>()], int n = pf<word_bw>()) {
        #pragma HLS INLINE
        write_stream_elem_impl(word_bw_tag<word_bw>{}, s, in, n);
    }

    template<int word_bw>
    static void write_axi4_stream_elem_impl(word_bw_tag<word_bw>, hls::stream<streamutils::axi4s_word<word_bw>>& s, const ap_uint<64>* in, bool tlast, int n) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream_elem");
        (void)s;
        (void)in;
        (void)tlast;
        (void)n;
    }

    static void write_axi4_stream_elem_impl(word_bw_tag<64>, hls::stream<streamutils::axi4s_word<64>>& s, const ap_uint<64>* in, bool tlast, int n) {
        #pragma HLS INLINE
        if (n > 0) {
            ap_uint<64> w = in[0];
            streamutils::write_axi4_word<64>(s, w, tlast);
        }
    }

    template<int word_bw>
    static void write_axi4_stream_elem(hls::stream<streamutils::axi4s_word<word_bw>>& s, const ap_uint<64> in[pf<word_bw>()], bool tlast = false, int n = pf<word_bw>()) {
        #pragma HLS INLINE
        write_axi4_stream_elem_impl(word_bw_tag<word_bw>{}, s, in, tlast, n);
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const UInt64Array* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<64>, const UInt64Array* self, ap_uint<64> x[]) {
        {
            const int n0_eff = 64;
            int out_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                x[out_idx++] = self->data[i0];
            }
        }
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const UInt64Array* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<64>, const UInt64Array* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 64;
            int out_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = self->data[i0];
                s.write(w);
                out_idx++;
            }
        }
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const UInt64Array* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const UInt64Array* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 64;
            int out_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = self->data[i0];
                streamutils::write_axi4_word<64>(s, w, tlast);
                out_idx++;
            }
        }
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, UInt64Array* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<64>, UInt64Array* self, const ap_uint<64> x[]) {
        {
            const int n0_eff = 64;
            int in_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                self->data[i0] = (ap_uint<64>)(x[in_idx]);
                in_idx++;
            }
        }
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, UInt64Array* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<64>, UInt64Array* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 64;
            int in_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = s.read();
                self->data[i0] = (ap_uint<64>)(w);
                in_idx++;
            }
        }
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, UInt64Array* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, UInt64Array* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
            ap_uint<64> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        {
            const int n0_eff = 64;
            int in_idx = 0;
            int elem_idx = 0;
            bool stop = false;
            for (int i0 = 0; i0 < n0_eff && !stop; ++i0) {
                {
                    auto axis_word = s.read();
                    w = axis_word.data;
                    last = axis_word.last;
                }
                self->data[i0] = (ap_uint<64>)(w);
                in_idx++;
                elem_idx++;
                if (last && elem_idx < (n0_eff)) {
                    stop = true;
                }
            }
            if (stop) {
                tl = streamutils::tlast_status::tlast_early;
                return;
            }
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    template<int word_bw>
    void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        read_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tl);
    }

    template<int word_bw>
    void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s) {
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        read_axi4_stream<word_bw>(s, tl);
    }

#ifdef WAVEFLOW_ENABLE_U_INT64_ARRAY_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_U_INT64_ARRAY_H