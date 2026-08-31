#ifndef INCLUDE_RF_SHOT_TX_HDR_H
#define INCLUDE_RF_SHOT_TX_HDR_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct ShotTxHdr {
    ap_uint<8> opcode;  // SHOT_LOAD or SHOT_END
    ap_uint<16> tid;  // transaction id, echoed on the response
    ap_uint<16> nsamp;  // samples the host is sending (0 for END)
    ap_uint<16> nrepeat;  // times to play the shot once loaded (>= 1)

    static constexpr int bitwidth = 56;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 1;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const ShotTxHdr& data) {
        ap_uint<bitwidth> res = 0;
        res.range(7, 0) = data.opcode;
        res.range(23, 8) = data.tid;
        res.range(39, 24) = data.nsamp;
        res.range(55, 40) = data.nrepeat;
        return res;
    }

    static ShotTxHdr unpack_from_uint(const ap_uint<bitwidth>& packed) {
        ShotTxHdr data;
        data.opcode = (ap_uint<8>)(packed.range(7, 0));
        data.tid = (ap_uint<16>)(packed.range(23, 8));
        data.nsamp = (ap_uint<16>)(packed.range(39, 24));
        data.nrepeat = (ap_uint<16>)(packed.range(55, 40));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const ShotTxHdr* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<64>, const ShotTxHdr* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(7, 0) = self->opcode;
        x[0].range(23, 8) = self->tid;
        x[0].range(39, 24) = self->nsamp;
        x[0].range(55, 40) = self->nrepeat;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const ShotTxHdr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<64>, const ShotTxHdr* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(7, 0) = self->opcode;
        w.range(23, 8) = self->tid;
        w.range(39, 24) = self->nsamp;
        w.range(55, 40) = self->nrepeat;
        s.write(w);
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const ShotTxHdr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const ShotTxHdr* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(7, 0) = self->opcode;
        w.range(23, 8) = self->tid;
        w.range(39, 24) = self->nsamp;
        w.range(55, 40) = self->nrepeat;
        streamutils::write_axi4_word<64>(s, w, tlast);
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, ShotTxHdr* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<64>, ShotTxHdr* self, const ap_uint<64> x[]) {
        self->opcode = (ap_uint<8>)(x[0].range(7, 0));
        self->tid = (ap_uint<16>)(x[0].range(23, 8));
        self->nsamp = (ap_uint<16>)(x[0].range(39, 24));
        self->nrepeat = (ap_uint<16>)(x[0].range(55, 40));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, ShotTxHdr* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<64>, ShotTxHdr* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->opcode = (ap_uint<8>)(w.range(7, 0));
        self->tid = (ap_uint<16>)(w.range(23, 8));
        self->nsamp = (ap_uint<16>)(w.range(39, 24));
        self->nrepeat = (ap_uint<16>)(w.range(55, 40));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, ShotTxHdr* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, ShotTxHdr* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->opcode = (ap_uint<8>)(w.range(7, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->tid = (ap_uint<16>)(w.range(23, 8));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nsamp = (ap_uint<16>)(w.range(39, 24));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->nrepeat = (ap_uint<16>)(w.range(55, 40));
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

#ifdef WAVEFLOW_ENABLE_RF_SHOT_TX_HDR_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_RF_SHOT_TX_HDR_H