#ifndef INCLUDE_BRAM_WRITE_COMPUTE_CMD_H
#define INCLUDE_BRAM_WRITE_COMPUTE_CMD_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "bram_op.h"

struct WriteComputeCmd {
    ap_uint<64> tid;  // transaction id, echoed on the response
    BramOp opcode;  // WRITE (payload in) or COMPUTE (in place)
    // extent in words; payload words for WRITE, none for COMPUTE
    ap_uint<64> nsamp;
    ap_uint<64> waddr;  // first word address touched

    static constexpr int bitwidth = 256;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 4;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const WriteComputeCmd& data) {
        ap_uint<bitwidth> res = 0;
        res.range(63, 0) = data.tid;
        res.range(127, 64) = (ap_uint<64>)(static_cast<unsigned int>(data.opcode));
        res.range(191, 128) = data.nsamp;
        res.range(255, 192) = data.waddr;
        return res;
    }

    static WriteComputeCmd unpack_from_uint(const ap_uint<bitwidth>& packed) {
        WriteComputeCmd data;
        data.tid = (ap_uint<64>)(packed.range(63, 0));
        data.opcode = static_cast<BramOp>(static_cast<unsigned int>(packed.range(127, 64)));
        data.nsamp = (ap_uint<64>)(packed.range(191, 128));
        data.waddr = (ap_uint<64>)(packed.range(255, 192));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const WriteComputeCmd* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<64>, const WriteComputeCmd* self, ap_uint<64> x[]) {
        x[0] = self->tid;
        x[1] = (ap_uint<64>)(static_cast<unsigned int>(self->opcode));
        x[2] = self->nsamp;
        x[3] = self->waddr;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const WriteComputeCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<64>, const WriteComputeCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = self->tid;
        s.write(w);
        w = 0;
        w = (ap_uint<64>)(static_cast<unsigned int>(self->opcode));
        s.write(w);
        w = 0;
        w = self->nsamp;
        s.write(w);
        w = 0;
        w = self->waddr;
        s.write(w);
        w = 0;
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const WriteComputeCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const WriteComputeCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w = self->tid;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w = (ap_uint<64>)(static_cast<unsigned int>(self->opcode));
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w = self->nsamp;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w = self->waddr;
        streamutils::write_axi4_word<64>(s, w, tlast);
        w = 0;
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, WriteComputeCmd* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<64>, WriteComputeCmd* self, const ap_uint<64> x[]) {
        self->tid = (ap_uint<64>)(x[0]);
        self->opcode = static_cast<BramOp>(static_cast<unsigned int>(x[1]));
        self->nsamp = (ap_uint<64>)(x[2]);
        self->waddr = (ap_uint<64>)(x[3]);
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, WriteComputeCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<64>, WriteComputeCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->tid = (ap_uint<64>)(w);
        w = s.read();
        self->opcode = static_cast<BramOp>(static_cast<unsigned int>(w));
        w = s.read();
        self->nsamp = (ap_uint<64>)(w);
        w = s.read();
        self->waddr = (ap_uint<64>)(w);
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, WriteComputeCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, WriteComputeCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->tid = (ap_uint<64>)(w);
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
        self->opcode = static_cast<BramOp>(static_cast<unsigned int>(w));
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
        self->nsamp = (ap_uint<64>)(w);
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
        self->waddr = (ap_uint<64>)(w);
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

#ifdef WAVEFLOW_ENABLE_BRAM_WRITE_COMPUTE_CMD_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_BRAM_WRITE_COMPUTE_CMD_H