#ifndef INCLUDE_MEM_COMPLETE_H
#define INCLUDE_MEM_COMPLETE_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "u_int32_array.h"

struct MemComplete {
    ap_uint<32> len;  // number of words transferred
    ap_uint<32> xfer_len;  // valid length of the echoed xfer_msg payload
    UInt32Array xfer_msg;  // the command's xfer_msg, echoed back unmodified

    static constexpr int bitwidth = 320;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 10;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 5;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const MemComplete& data) {
        ap_uint<bitwidth> res = 0;
        res.range(31, 0) = data.len;
        res.range(63, 32) = data.xfer_len;
        res.range(319, 64) = UInt32Array::pack_to_uint(data.xfer_msg);
        return res;
    }

    static MemComplete unpack_from_uint(const ap_uint<bitwidth>& packed) {
        MemComplete data;
        data.len = (ap_uint<32>)(packed.range(31, 0));
        data.xfer_len = (ap_uint<32>)(packed.range(63, 32));
        data.xfer_msg = UInt32Array::unpack_from_uint(packed.range(319, 64));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const MemComplete* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const MemComplete* self, ap_uint<32> x[]) {
        x[0] = self->len;
        x[1] = self->xfer_len;
        {
            const int n0_eff = 8;
            int out_idx = 2;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                x[out_idx++] = self->xfer_msg.data[i0];
            }
        }
    }

    static void write_array_impl(word_bw_tag<64>, const MemComplete* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(31, 0) = self->len;
        x[0].range(63, 32) = self->xfer_len;
        {
            const int n0_eff = 8;
            int out_idx = 1;
            for (int i = 0; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                ap_uint<64> w = 0;
                if (i + 0 < n0_eff) {
                    w.range(31, 0) = self->xfer_msg.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(63, 32) = self->xfer_msg.data[i + 1];
                }
                x[out_idx++] = w;
            }
        }
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const MemComplete* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const MemComplete* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = self->len;
        s.write(w);
        w = 0;
        w = self->xfer_len;
        s.write(w);
        w = 0;
        {
            const int n0_eff = 8;
            int out_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = self->xfer_msg.data[i0];
                s.write(w);
                out_idx++;
            }
        }
    }

    static void write_stream_impl(word_bw_tag<64>, const MemComplete* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->len;
        w.range(63, 32) = self->xfer_len;
        s.write(w);
        w = 0;
        {
            const int n0_eff = 8;
            int out_idx = 0;
            for (int i = 0; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                w = 0;
                if (i + 0 < n0_eff) {
                    w.range(31, 0) = self->xfer_msg.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(63, 32) = self->xfer_msg.data[i + 1];
                }
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
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const MemComplete* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const MemComplete* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w = self->len;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        w = self->xfer_len;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
        {
            const int n0_eff = 8;
            int out_idx = 0;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = self->xfer_msg.data[i0];
                streamutils::write_axi4_word<32>(s, w, tlast);
                out_idx++;
            }
        }
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const MemComplete* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->len;
        w.range(63, 32) = self->xfer_len;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        {
            const int n0_eff = 8;
            int out_idx = 0;
            for (int i = 0; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                w = 0;
                if (i + 0 < n0_eff) {
                    w.range(31, 0) = self->xfer_msg.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(63, 32) = self->xfer_msg.data[i + 1];
                }
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
    static void read_array_impl(word_bw_tag<word_bw>, MemComplete* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, MemComplete* self, const ap_uint<32> x[]) {
        self->len = (ap_uint<32>)(x[0]);
        self->xfer_len = (ap_uint<32>)(x[1]);
        {
            const int n0_eff = 8;
            int in_idx = 2;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                self->xfer_msg.data[i0] = (ap_uint<32>)(x[in_idx]);
                in_idx++;
            }
        }
    }

    static void read_array_impl(word_bw_tag<64>, MemComplete* self, const ap_uint<64> x[]) {
        self->len = (ap_uint<32>)(x[0].range(31, 0));
        self->xfer_len = (ap_uint<32>)(x[0].range(63, 32));
        {
            const int n0_eff = 8;
            int in_idx = 1;
            for (int i = 0; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                ap_uint<64> w = x[in_idx++];
                if (i + 0 < n0_eff) {
                    self->xfer_msg.data[i + 0] = (ap_uint<32>)(w.range(31, 0));
                }
                if (i + 1 < n0_eff) {
                    self->xfer_msg.data[i + 1] = (ap_uint<32>)(w.range(63, 32));
                }
            }
        }
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, MemComplete* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, MemComplete* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->len = (ap_uint<32>)(w);
        w = s.read();
        self->xfer_len = (ap_uint<32>)(w);
        {
            const int n0_eff = 8;
            int in_idx = 2;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = s.read();
                self->xfer_msg.data[i0] = (ap_uint<32>)(w);
                in_idx++;
            }
        }
    }

    static void read_stream_impl(word_bw_tag<64>, MemComplete* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->len = (ap_uint<32>)(w.range(31, 0));
        self->xfer_len = (ap_uint<32>)(w.range(63, 32));
        {
            const int n0_eff = 8;
            int in_idx = 1;
            for (int i = 0; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                w = s.read();
                in_idx++;
                if (i + 0 < n0_eff) {
                    self->xfer_msg.data[i + 0] = (ap_uint<32>)(w.range(31, 0));
                }
                if (i + 1 < n0_eff) {
                    self->xfer_msg.data[i + 1] = (ap_uint<32>)(w.range(63, 32));
                }
            }
        }
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, MemComplete* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, MemComplete* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->xfer_len = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            const int n0_eff = 8;
            int in_idx = 2;
            int elem_idx = 0;
            bool stop = false;
            for (int i0 = 0; i0 < n0_eff && !stop; ++i0) {
                {
                    auto axis_word = s.read();
                    w = axis_word.data;
                    last = axis_word.last;
                }
                self->xfer_msg.data[i0] = (ap_uint<32>)(w);
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
        if (tl != streamutils::tlast_status::no_tlast) {
            return;
        }
        if (last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, MemComplete* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->len = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->xfer_len = (ap_uint<32>)(w.range(63, 32));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            const int n0_eff = 8;
            int in_idx = 1;
            int i = 0;
            for (; i < n0_eff; i += 2) {
                #pragma HLS PIPELINE II=1
                if (last) {
                    break;
                }
                {
                    auto axis_word = s.read();
                    w = axis_word.data;
                    last = axis_word.last;
                }
                in_idx++;
                if (i + 0 < n0_eff) {
                    self->xfer_msg.data[i + 0] = (ap_uint<32>)(w.range(31, 0));
                }
                if (i + 1 < n0_eff) {
                    self->xfer_msg.data[i + 1] = (ap_uint<32>)(w.range(63, 32));
                }
                if (last) {
                    break;
                }
            }
            if ((i + 2) < n0_eff) {
                tl = streamutils::tlast_status::tlast_early;
                return;
            }
        }
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

#ifdef WAVEFLOW_ENABLE_MEM_COMPLETE_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_MEM_COMPLETE_H