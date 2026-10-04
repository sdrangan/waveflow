#ifndef INCLUDE_FIR_CFG_H
#define INCLUDE_FIR_CFG_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

#include "int16_array.h"

struct FirCfg {
    Int16Array coeffs;  // tap k multiplies x[n-k]
    ap_uint<32> ntaps;  // active taps (<= NTAP_MAX)
    // the host's name for this config, 1..65535 (0 = no config yet); packets ask for it by this id
    ap_uint<16> cfg_id;

    static constexpr int bitwidth = 304;

    template<int word_bw>
    struct word_bw_tag {};

    template<int word_bw>
    static constexpr int nwords_value(word_bw_tag<word_bw>) {
            static_assert(word_bw < 0, "Unsupported word_bw for nwords");
            return 0;
    }

    static constexpr int nwords_value(word_bw_tag<64>) {
            return 5;
    }

    template<int word_bw>
    static constexpr int nwords() {
        return nwords_value(word_bw_tag<word_bw>{});
    }

    static ap_uint<bitwidth> pack_to_uint(const FirCfg& data) {
        ap_uint<bitwidth> res = 0;
        res.range(255, 0) = Int16Array::pack_to_uint(data.coeffs);
        res.range(287, 256) = data.ntaps;
        res.range(303, 288) = data.cfg_id;
        return res;
    }

    static FirCfg unpack_from_uint(const ap_uint<bitwidth>& packed) {
        FirCfg data;
        data.coeffs = Int16Array::unpack_from_uint(packed.range(255, 0));
        data.ntaps = (ap_uint<32>)(packed.range(287, 256));
        data.cfg_id = (ap_uint<16>)(packed.range(303, 288));
        return data;
    }

    template<int word_bw>
    static void write_array_impl(word_bw_tag<word_bw>, const FirCfg* self, ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_array");
        (void)self;
        (void)x;
    }

    static void write_array_impl(word_bw_tag<64>, const FirCfg* self, ap_uint<64> x[]) {
        {
            const int n0_eff = 16;
            int out_idx = 0;
            for (int i = 0; i < n0_eff; i += 4) {
                #pragma HLS PIPELINE II=1
                ap_uint<64> w = 0;
                if (i + 0 < n0_eff) {
                    w.range(15, 0) = self->coeffs.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(31, 16) = self->coeffs.data[i + 1];
                }
                if (i + 2 < n0_eff) {
                    w.range(47, 32) = self->coeffs.data[i + 2];
                }
                if (i + 3 < n0_eff) {
                    w.range(63, 48) = self->coeffs.data[i + 3];
                }
                x[out_idx++] = w;
            }
        }
        x[4] = 0;
        x[4].range(31, 0) = self->ntaps;
        x[4].range(47, 32) = self->cfg_id;
    }

    template<int word_bw>
    void write_array(ap_uint<word_bw> x[]) const {
        write_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void write_stream_impl(word_bw_tag<word_bw>, const FirCfg* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_stream");
        (void)self;
        (void)s;
    }

    static void write_stream_impl(word_bw_tag<64>, const FirCfg* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 16;
            int out_idx = 0;
            for (int i = 0; i < n0_eff; i += 4) {
                #pragma HLS PIPELINE II=1
                w = 0;
                if (i + 0 < n0_eff) {
                    w.range(15, 0) = self->coeffs.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(31, 16) = self->coeffs.data[i + 1];
                }
                if (i + 2 < n0_eff) {
                    w.range(47, 32) = self->coeffs.data[i + 2];
                }
                if (i + 3 < n0_eff) {
                    w.range(63, 48) = self->coeffs.data[i + 3];
                }
                s.write(w);
                out_idx++;
            }
        }
        w.range(31, 0) = self->ntaps;
        w.range(47, 32) = self->cfg_id;
        s.write(w);
    }

    template<int word_bw>
    void write_stream(hls::stream<ap_uint<word_bw>> &s) const {
        write_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void write_axi4_stream_impl(word_bw_tag<word_bw>, const FirCfg* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast) {
        static_assert(word_bw < 0, "Unsupported word_bw for write_axi4_stream");
        (void)self;
        (void)s;
        (void)tlast;
    }

    static void write_axi4_stream_impl(word_bw_tag<64>, const FirCfg* self, hls::stream<streamutils::axi4s_word<64>> &s, bool tlast) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 16;
            int out_idx = 0;
            for (int i = 0; i < n0_eff; i += 4) {
                #pragma HLS PIPELINE II=1
                w = 0;
                if (i + 0 < n0_eff) {
                    w.range(15, 0) = self->coeffs.data[i + 0];
                }
                if (i + 1 < n0_eff) {
                    w.range(31, 16) = self->coeffs.data[i + 1];
                }
                if (i + 2 < n0_eff) {
                    w.range(47, 32) = self->coeffs.data[i + 2];
                }
                if (i + 3 < n0_eff) {
                    w.range(63, 48) = self->coeffs.data[i + 3];
                }
                streamutils::write_axi4_word<64>(s, w, false);
                out_idx++;
            }
        }
        w.range(31, 0) = self->ntaps;
        w.range(47, 32) = self->cfg_id;
        streamutils::write_axi4_word<64>(s, w, tlast);
    }

    template<int word_bw>
    void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>> &s, bool tlast = true) const {
        write_axi4_stream_impl(word_bw_tag<word_bw>{}, this, s, tlast);
    }

    template<int word_bw>
    static void read_array_impl(word_bw_tag<word_bw>, FirCfg* self, const ap_uint<word_bw> x[]) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_array");
        (void)self;
        (void)x;
    }

    static void read_array_impl(word_bw_tag<64>, FirCfg* self, const ap_uint<64> x[]) {
        {
            const int n0_eff = 16;
            int in_idx = 0;
            for (int i = 0; i < n0_eff; i += 4) {
                #pragma HLS PIPELINE II=1
                ap_uint<64> w = x[in_idx++];
                if (i + 0 < n0_eff) {
                    self->coeffs.data[i + 0] = (ap_int<16>)(w.range(15, 0));
                }
                if (i + 1 < n0_eff) {
                    self->coeffs.data[i + 1] = (ap_int<16>)(w.range(31, 16));
                }
                if (i + 2 < n0_eff) {
                    self->coeffs.data[i + 2] = (ap_int<16>)(w.range(47, 32));
                }
                if (i + 3 < n0_eff) {
                    self->coeffs.data[i + 3] = (ap_int<16>)(w.range(63, 48));
                }
            }
        }
        self->ntaps = (ap_uint<32>)(x[4].range(31, 0));
        self->cfg_id = (ap_uint<16>)(x[4].range(47, 32));
    }

    template<int word_bw>
    void read_array(const ap_uint<word_bw> x[]) {
        read_array_impl(word_bw_tag<word_bw>{}, this, x);
    }

    template<int word_bw>
    static void read_stream_impl(word_bw_tag<word_bw>, FirCfg* self, hls::stream<ap_uint<word_bw>> &s) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_stream");
        (void)self;
        (void)s;
    }

    static void read_stream_impl(word_bw_tag<64>, FirCfg* self, hls::stream<ap_uint<64>> &s) {
            ap_uint<64> w = 0;
        {
            const int n0_eff = 16;
            int in_idx = 0;
            for (int i = 0; i < n0_eff; i += 4) {
                #pragma HLS PIPELINE II=1
                w = s.read();
                in_idx++;
                if (i + 0 < n0_eff) {
                    self->coeffs.data[i + 0] = (ap_int<16>)(w.range(15, 0));
                }
                if (i + 1 < n0_eff) {
                    self->coeffs.data[i + 1] = (ap_int<16>)(w.range(31, 16));
                }
                if (i + 2 < n0_eff) {
                    self->coeffs.data[i + 2] = (ap_int<16>)(w.range(47, 32));
                }
                if (i + 3 < n0_eff) {
                    self->coeffs.data[i + 3] = (ap_int<16>)(w.range(63, 48));
                }
            }
        }
        w = s.read();
        self->ntaps = (ap_uint<32>)(w.range(31, 0));
        self->cfg_id = (ap_uint<16>)(w.range(47, 32));
    }

    template<int word_bw>
    void read_stream(hls::stream<ap_uint<word_bw>> &s) {
        read_stream_impl(word_bw_tag<word_bw>{}, this, s);
    }

    template<int word_bw>
    static void read_axi4_stream_impl(word_bw_tag<word_bw>, FirCfg* self, hls::stream<streamutils::axi4s_word<word_bw>> &s, streamutils::tlast_status &tl) {
        static_assert(word_bw < 0, "Unsupported word_bw for read_axi4_stream");
        (void)self;
        (void)s;
        (void)tl;
    }

    static void read_axi4_stream_impl(word_bw_tag<64>, FirCfg* self, hls::stream<streamutils::axi4s_word<64>> &s, streamutils::tlast_status &tl) {
            ap_uint<64> w = 0;
            tl = streamutils::tlast_status::no_tlast;
            bool last = false;
        {
            const int n0_eff = 16;
            int in_idx = 0;
            int i = 0;
            for (; i < n0_eff; i += 4) {
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
                    self->coeffs.data[i + 0] = (ap_int<16>)(w.range(15, 0));
                }
                if (i + 1 < n0_eff) {
                    self->coeffs.data[i + 1] = (ap_int<16>)(w.range(31, 16));
                }
                if (i + 2 < n0_eff) {
                    self->coeffs.data[i + 2] = (ap_int<16>)(w.range(47, 32));
                }
                if (i + 3 < n0_eff) {
                    self->coeffs.data[i + 3] = (ap_int<16>)(w.range(63, 48));
                }
                if (last) {
                    break;
                }
            }
            if ((i + 4) < n0_eff) {
                tl = streamutils::tlast_status::tlast_early;
                return;
            }
        }
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
        self->ntaps = (ap_uint<32>)(w.range(31, 0));
        if (tl != streamutils::tlast_status::no_tlast) {
            tl = streamutils::tlast_status::tlast_early;
            return;
        }
        self->cfg_id = (ap_uint<16>)(w.range(47, 32));
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

#ifdef WAVEFLOW_ENABLE_FIR_CFG_TB_H_MEMBERS
    void dump_json(std::ostream& os, int indent = 2, int level = 0) const;
    void load_json(const std::string& json_text, size_t& pos);
    void load_json(std::istream& is);
    void dump_json_file(const char* file_path, int indent = 2) const;
    void load_json_file(const char* file_path);
#endif
};

#endif // INCLUDE_FIR_CFG_H