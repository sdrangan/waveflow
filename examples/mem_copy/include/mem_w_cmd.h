#ifndef INCLUDE_MEM_W_CMD_H
#define INCLUDE_MEM_W_CMD_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

struct MemWCmd {
    ap_uint<32> addr;  // element/word offset to write at
    ap_uint<32> len;  // number of packed data words to write
    // opaque bursts to buffer across the write, then echo on s_done
    ap_uint<32> fwd_bursts;

    static constexpr int bitwidth = 96;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 3;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 2;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const MemWCmd& data) {
        ap_uint<bitwidth> res = 0;
        res.range(31, 0) = data.addr;
        res.range(63, 32) = data.len;
        res.range(95, 64) = data.fwd_bursts;
        return res;
    }

    static MemWCmd unpack_from_uint(const ap_uint<bitwidth>& packed) {
        MemWCmd data;
        data.addr = (ap_uint<32>)(packed.range(31, 0));
        data.len = (ap_uint<32>)(packed.range(63, 32));
        data.fwd_bursts = (ap_uint<32>)(packed.range(95, 64));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const MemWCmd* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const MemWCmd* self, ap_uint<32> x[]) {
        x[0] = self->addr;
        x[1] = self->len;
        x[2] = self->fwd_bursts;
    }

    static void write_array_impl(word_bw_tag<64>, const MemWCmd* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(31, 0) = self->addr;
        x[0].range(63, 32) = self->len;
        x[1] = 0;
        x[1].range(31, 0) = self->fwd_bursts;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const MemWCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const MemWCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = self->addr;
        s.write(w);
        w = 0;
        w = self->len;
        s.write(w);
        w = 0;
        w = self->fwd_bursts;
        s.write(w);
        w = 0;
    }

    static void write_stream_impl(word_bw_tag<64>, const MemWCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->addr;
        w.range(63, 32) = self->len;
        s.write(w);
        w = 0;
        w.range(31, 0) = self->fwd_bursts;
        s.write(w);
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const MemWCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const MemWCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w = self->addr;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->len;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->fwd_bursts;
        streamutils::write_axi4_word<32>(s, w, tlast);
        w = 0;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const MemWCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->addr;
        w.range(63, 32) = self->len;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w.range(31, 0) = self->fwd_bursts;
        streamutils::write_axi4_word<64>(s, w, tlast);
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void write_framed_stream_impl(word_bw_tag<word_bw>, const MemWCmd* self, hls::stream<streamutils::framed_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_framed_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_framed_stream_impl(word_bw_tag<32>, const MemWCmd* self, hls::stream<streamutils::framed_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w = self->addr;
        streamutils::write_boundary_word<streamutils::framed_word<32>, 32>(s, w, false);
        w = 0;
        w = self->len;
        streamutils::write_boundary_word<streamutils::framed_word<32>, 32>(s, w, false);
        w = 0;
        w = self->fwd_bursts;
        streamutils::write_boundary_word<streamutils::framed_word<32>, 32>(s, w, tlast);
        w = 0;
    }

    static void write_framed_stream_impl(word_bw_tag<64>, const MemWCmd* self, hls::stream<streamutils::framed_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->addr;
        w.range(63, 32) = self->len;
        streamutils::write_boundary_word<streamutils::framed_word<64>, 64>(s, w, false);
        w = 0;
        w.range(31, 0) = self->fwd_bursts;
        streamutils::write_boundary_word<streamutils::framed_word<64>, 64>(s, w, tlast);
    }

    template<int word_bw>
    void write_framed_stream(hls::stream<streamutils::framed_word<word_bw>> &s, bool tlast = true) const {
        write_framed_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, MemWCmd* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, MemWCmd* self, const ap_uint<32> x[]) {
        self->addr = (ap_uint<32>)(x[0]);
        self->len = (ap_uint<32>)(x[1]);
        self->fwd_bursts = (ap_uint<32>)(x[2]);
    }

    static void read_array_impl(word_bw_tag<64>, MemWCmd* self, const ap_uint<64> x[]) {
        self->addr = (ap_uint<32>)(x[0].range(31, 0));
        self->len = (ap_uint<32>)(x[0].range(63, 32));
        self->fwd_bursts = (ap_uint<32>)(x[1].range(31, 0));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, MemWCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, MemWCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->addr = (ap_uint<32>)(w);
        w = s.read();
        self->len = (ap_uint<32>)(w);
        w = s.read();
        self->fwd_bursts = (ap_uint<32>)(w);
    }

    static void read_stream_impl(word_bw_tag<64>, MemWCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->addr = (ap_uint<32>)(w.range(31, 0));
        self->len = (ap_uint<32>)(w.range(63, 32));
        w = s.read();
        self->fwd_bursts = (ap_uint<32>)(w.range(31, 0));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, MemWCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, MemWCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->addr = (ap_uint<32>)(w);
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
        self->len = (ap_uint<32>)(w);
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
        self->fwd_bursts = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, MemWCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->addr = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->len = (ap_uint<32>)(w.range(63, 32));
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
        self->fwd_bursts = (ap_uint<32>)(w.range(31, 0));
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

    template<int word_bw>
    static void read_framed_stream_impl(word_bw_tag<word_bw>, MemWCmd* self, hls::stream<streamutils::framed_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_framed_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_framed_stream_impl(word_bw_tag<32>, MemWCmd* self, hls::stream<streamutils::framed_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->addr = (ap_uint<32>)(w);
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
        self->len = (ap_uint<32>)(w);
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
        self->fwd_bursts = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_framed_stream_impl(word_bw_tag<64>, MemWCmd* self, hls::stream<streamutils::framed_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->addr = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->len = (ap_uint<32>)(w.range(63, 32));
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
        self->fwd_bursts = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    template<int word_bw>
    void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>> &s, streamutils::tlast_status &tl) {
        read_framed_stream_impl(word_bw_tag<word_bw>{}, this, s, tl);
    }

    template<int word_bw>
    void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>> &s) {
        streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
        read_framed_stream<word_bw>(s, tl);
    }

#ifdef WAVEFLOW_ENABLE_MEM_W_CMD_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_MEM_W_CMD_H