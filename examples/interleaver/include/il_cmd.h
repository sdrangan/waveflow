#ifndef INCLUDE_IL_CMD_H
#define INCLUDE_IL_CMD_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct InterleaverCmd {
    ap_uint<32> p_off;  // P (index) buffer word offset
    ap_uint<32> x_off;  // X (source) buffer word offset
    ap_uint<32> y_off;  // Y (output) buffer word offset
    ap_uint<32> n;  // number of elements to interleave

    static constexpr int bitwidth = 128;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 4;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 2;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const InterleaverCmd& data) {
        ap_uint<bitwidth> res = 0;
        res.range(31, 0) = data.p_off;
        res.range(63, 32) = data.x_off;
        res.range(95, 64) = data.y_off;
        res.range(127, 96) = data.n;
        return res;
    }

    static InterleaverCmd unpack_from_uint(const ap_uint<bitwidth>& packed) {
        InterleaverCmd data;
        data.p_off = (ap_uint<32>)(packed.range(31, 0));
        data.x_off = (ap_uint<32>)(packed.range(63, 32));
        data.y_off = (ap_uint<32>)(packed.range(95, 64));
        data.n = (ap_uint<32>)(packed.range(127, 96));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const InterleaverCmd* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const InterleaverCmd* self, ap_uint<32> x[]) {
        x[0] = self->p_off;
        x[1] = self->x_off;
        x[2] = self->y_off;
        x[3] = self->n;
    }

    static void write_array_impl(word_bw_tag<64>, const InterleaverCmd* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(31, 0) = self->p_off;
        x[0].range(63, 32) = self->x_off;
        x[1] = 0;
        x[1].range(31, 0) = self->y_off;
        x[1].range(63, 32) = self->n;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const InterleaverCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const InterleaverCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = self->p_off;
        s.write(w);
        w = 0;
        w = self->x_off;
        s.write(w);
        w = 0;
        w = self->y_off;
        s.write(w);
        w = 0;
        w = self->n;
        s.write(w);
        w = 0;
    }

    static void write_stream_impl(word_bw_tag<64>, const InterleaverCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->p_off;
        w.range(63, 32) = self->x_off;
        s.write(w);
        w = 0;
        w.range(31, 0) = self->y_off;
        w.range(63, 32) = self->n;
        s.write(w);
        w = 0;
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const InterleaverCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const InterleaverCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w = self->p_off;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->x_off;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->y_off;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->n;
        streamutils::write_axi4_word<32>(s, w, tlast);
        w = 0;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const InterleaverCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->p_off;
        w.range(63, 32) = self->x_off;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w.range(31, 0) = self->y_off;
        w.range(63, 32) = self->n;
        streamutils::write_axi4_word<64>(s, w, tlast);
        w = 0;
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, InterleaverCmd* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, InterleaverCmd* self, const ap_uint<32> x[]) {
        self->p_off = (ap_uint<32>)(x[0]);
        self->x_off = (ap_uint<32>)(x[1]);
        self->y_off = (ap_uint<32>)(x[2]);
        self->n = (ap_uint<32>)(x[3]);
    }

    static void read_array_impl(word_bw_tag<64>, InterleaverCmd* self, const ap_uint<64> x[]) {
        self->p_off = (ap_uint<32>)(x[0].range(31, 0));
        self->x_off = (ap_uint<32>)(x[0].range(63, 32));
        self->y_off = (ap_uint<32>)(x[1].range(31, 0));
        self->n = (ap_uint<32>)(x[1].range(63, 32));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, InterleaverCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, InterleaverCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->p_off = (ap_uint<32>)(w);
        w = s.read();
        self->x_off = (ap_uint<32>)(w);
        w = s.read();
        self->y_off = (ap_uint<32>)(w);
        w = s.read();
        self->n = (ap_uint<32>)(w);
    }

    static void read_stream_impl(word_bw_tag<64>, InterleaverCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->p_off = (ap_uint<32>)(w.range(31, 0));
        self->x_off = (ap_uint<32>)(w.range(63, 32));
        w = s.read();
        self->y_off = (ap_uint<32>)(w.range(31, 0));
        self->n = (ap_uint<32>)(w.range(63, 32));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, InterleaverCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, InterleaverCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
            ap_uint<32> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->p_off = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->x_off = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->y_off = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->n = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, InterleaverCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
            ap_uint<64> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->p_off = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->x_off = (ap_uint<32>)(w.range(63, 32));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            auto axis_word = s.read();
            w = axis_word.data;
            last = axis_word.last;
        }
        self->y_off = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->n = (ap_uint<32>)(w.range(63, 32));
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
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

#ifdef WAVEFLOW_ENABLE_IL_CMD_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_IL_CMD_H