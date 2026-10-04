#ifndef INCLUDE_MKV_CMD_H
#define INCLUDE_MKV_CMD_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct MkvCmd {
    ap_uint<32> n;  // steps to run
    ap_uint<16> tx_id;  // the host's job id, echoed in the response
    ap_uint<16> x0;  // initial state, 0 or 1
    ap_uint<32> seed;  // xorshift32 seed (0 is replaced by 1)
    ap_uint<16> p01;  // P(0 -> 1) in Q16
    ap_uint<16> p10;  // P(1 -> 0) in Q16
    ap_uint<64> dstaddr;  // bus address x[0..n-1] is written to

    static constexpr int bitwidth = 192;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 3;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const MkvCmd& data) {
        ap_uint<bitwidth> res = 0;
        res.range(31, 0) = data.n;
        res.range(47, 32) = data.tx_id;
        res.range(63, 48) = data.x0;
        res.range(95, 64) = data.seed;
        res.range(111, 96) = data.p01;
        res.range(127, 112) = data.p10;
        res.range(191, 128) = data.dstaddr;
        return res;
    }

    static MkvCmd unpack_from_uint(const ap_uint<bitwidth>& packed) {
        MkvCmd data;
        data.n = (ap_uint<32>)(packed.range(31, 0));
        data.tx_id = (ap_uint<16>)(packed.range(47, 32));
        data.x0 = (ap_uint<16>)(packed.range(63, 48));
        data.seed = (ap_uint<32>)(packed.range(95, 64));
        data.p01 = (ap_uint<16>)(packed.range(111, 96));
        data.p10 = (ap_uint<16>)(packed.range(127, 112));
        data.dstaddr = (ap_uint<64>)(packed.range(191, 128));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const MkvCmd* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<64>, const MkvCmd* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(31, 0) = self->n;
        x[0].range(47, 32) = self->tx_id;
        x[0].range(63, 48) = self->x0;
        x[1] = 0;
        x[1].range(31, 0) = self->seed;
        x[1].range(47, 32) = self->p01;
        x[1].range(63, 48) = self->p10;
        x[2] = self->dstaddr;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const MkvCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<64>, const MkvCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->n;
        w.range(47, 32) = self->tx_id;
        w.range(63, 48) = self->x0;
        s.write(w);
        w = 0;
        w.range(31, 0) = self->seed;
        w.range(47, 32) = self->p01;
        w.range(63, 48) = self->p10;
        s.write(w);
        w = 0;
        w = self->dstaddr;
        s.write(w);
        w = 0;
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const MkvCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const MkvCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->n;
        w.range(47, 32) = self->tx_id;
        w.range(63, 48) = self->x0;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w.range(31, 0) = self->seed;
        w.range(47, 32) = self->p01;
        w.range(63, 48) = self->p10;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w = self->dstaddr;
        streamutils::write_axi4_word<64>(s, w, tlast);
        w = 0;
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, MkvCmd* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<64>, MkvCmd* self, const ap_uint<64> x[]) {
        self->n = (ap_uint<32>)(x[0].range(31, 0));
        self->tx_id = (ap_uint<16>)(x[0].range(47, 32));
        self->x0 = (ap_uint<16>)(x[0].range(63, 48));
        self->seed = (ap_uint<32>)(x[1].range(31, 0));
        self->p01 = (ap_uint<16>)(x[1].range(47, 32));
        self->p10 = (ap_uint<16>)(x[1].range(63, 48));
        self->dstaddr = (ap_uint<64>)(x[2]);
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, MkvCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<64>, MkvCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->n = (ap_uint<32>)(w.range(31, 0));
        self->tx_id = (ap_uint<16>)(w.range(47, 32));
        self->x0 = (ap_uint<16>)(w.range(63, 48));
        w = s.read();
        self->seed = (ap_uint<32>)(w.range(31, 0));
        self->p01 = (ap_uint<16>)(w.range(47, 32));
        self->p10 = (ap_uint<16>)(w.range(63, 48));
        w = s.read();
        self->dstaddr = (ap_uint<64>)(w);
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, MkvCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, MkvCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->n = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->tx_id = (ap_uint<16>)(w.range(47, 32));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->x0 = (ap_uint<16>)(w.range(63, 48));
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
        self->seed = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->p01 = (ap_uint<16>)(w.range(47, 32));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->p10 = (ap_uint<16>)(w.range(63, 48));
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
        self->dstaddr = (ap_uint<64>)(w);
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

#ifdef WAVEFLOW_ENABLE_MKV_CMD_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_MKV_CMD_H