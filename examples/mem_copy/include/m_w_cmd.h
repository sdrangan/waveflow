#ifndef INCLUDE_M_W_CMD_H
#define INCLUDE_M_W_CMD_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "u_int32_array.h"

struct MWCmd {
    ap_uint<32> addr;  // element/word offset within the bound buffer
    ap_uint<32> len;  // number of packed words to write
    ap_uint<32> xfer_len;  // active length of xfer_msg (<= max_xfer_len)
    // opaque per-job correlation cookie, round-tripped on completion
    UInt32Array xfer_msg;

    static constexpr int bitwidth = 352;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<32>) {
            return 11;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 6;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const MWCmd& data) {
        ap_uint<bitwidth> res = 0;
        res.range(31, 0) = data.addr;
        res.range(63, 32) = data.len;
        res.range(95, 64) = data.xfer_len;
        res.range(351, 96) = UInt32Array::pack_to_uint(data.xfer_msg);
        return res;
    }

    static MWCmd unpack_from_uint(const ap_uint<bitwidth>& packed) {
        MWCmd data;
        data.addr = (ap_uint<32>)(packed.range(31, 0));
        data.len = (ap_uint<32>)(packed.range(63, 32));
        data.xfer_len = (ap_uint<32>)(packed.range(95, 64));
        data.xfer_msg = UInt32Array::unpack_from_uint(packed.range(351, 96));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const MWCmd* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<32>, const MWCmd* self, ap_uint<32> x[]) {
        x[0] = self->addr;
        x[1] = self->len;
        x[2] = self->xfer_len;
        {
            const int n0_eff = 8;
            int out_idx = 3;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                x[out_idx++] = self->xfer_msg.data[i0];
            }
        }
    }

    static void write_array_impl(word_bw_tag<64>, const MWCmd* self, ap_uint<64> x[]) {
        x[0] = 0;
        x[0].range(31, 0) = self->addr;
        x[0].range(63, 32) = self->len;
        x[1] = 0;
        x[1].range(31, 0) = self->xfer_len;
        {
            const int n0_eff = 8;
            int out_idx = 2;
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
    static void write_stream_impl(word_bw_tag<word_bw>, const MWCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<32>, const MWCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = self->addr;
        s.write(w);
        w = 0;
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

    static void write_stream_impl(word_bw_tag<64>, const MWCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->addr;
        w.range(63, 32) = self->len;
        s.write(w);
        w = 0;
        w.range(31, 0) = self->xfer_len;
        {
            s.write(w);
            w = 0;
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
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const MWCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<32>, const MWCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, bool tlast) {
            ap_uint<32> w = 0;
        w = self->addr;
        streamutils::write_axi4_word<32>(s, w, false);
        w = 0;
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

    static void write_axi4_stream_impl(word_bw_tag<64>, const MWCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        w.range(31, 0) = self->addr;
        w.range(63, 32) = self->len;
        streamutils::write_axi4_word<64>(s, w, false);
        w = 0;
        w.range(31, 0) = self->xfer_len;
        {
            streamutils::write_axi4_word<64>(s, w, false);
            w = 0;
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
    static void read_array_impl(word_bw_tag<word_bw>, MWCmd* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<32>, MWCmd* self, const ap_uint<32> x[]) {
        self->addr = (ap_uint<32>)(x[0]);
        self->len = (ap_uint<32>)(x[1]);
        self->xfer_len = (ap_uint<32>)(x[2]);
        {
            const int n0_eff = 8;
            int in_idx = 3;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                self->xfer_msg.data[i0] = (ap_uint<32>)(x[in_idx]);
                in_idx++;
            }
        }
    }

    static void read_array_impl(word_bw_tag<64>, MWCmd* self, const ap_uint<64> x[]) {
        self->addr = (ap_uint<32>)(x[0].range(31, 0));
        self->len = (ap_uint<32>)(x[0].range(63, 32));
        self->xfer_len = (ap_uint<32>)(x[1].range(31, 0));
        {
            const int n0_eff = 8;
            int in_idx = 2;
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
    static void read_stream_impl(word_bw_tag<word_bw>, MWCmd* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<32>, MWCmd* self, hls::stream<ap_uint<32>> &s) {
            ap_uint<32> w = 0;
        w = s.read();
        self->addr = (ap_uint<32>)(w);
        w = s.read();
        self->len = (ap_uint<32>)(w);
        w = s.read();
        self->xfer_len = (ap_uint<32>)(w);
        {
            const int n0_eff = 8;
            int in_idx = 3;
            for (int i0 = 0; i0 < n0_eff; ++i0) {
                w = s.read();
                self->xfer_msg.data[i0] = (ap_uint<32>)(w);
                in_idx++;
            }
        }
    }

    static void read_stream_impl(word_bw_tag<64>, MWCmd* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        w = s.read();
        self->addr = (ap_uint<32>)(w.range(31, 0));
        self->len = (ap_uint<32>)(w.range(63, 32));
        w = s.read();
        self->xfer_len = (ap_uint<32>)(w.range(31, 0));
        {
            const int n0_eff = 8;
            int in_idx = 2;
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
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, MWCmd* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<32>, MWCmd* self, hls::stream<streamutils::axi4s_word<32>> &s, streamutils::tlast_status &tl) {
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
        self->xfer_len = (ap_uint<32>)(w);
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            const int n0_eff = 8;
            int in_idx = 3;
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

    static void read_axi4_stream_impl(word_bw_tag<64>, MWCmd* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
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
        self->xfer_len = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        {
            const int n0_eff = 8;
            int in_idx = 2;
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

#ifdef WAVEFLOW_ENABLE_M_W_CMD_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_M_W_CMD_H