#ifndef INCLUDE_IL_ELEM_ARRAY_UTILS_H
#define INCLUDE_IL_ELEM_ARRAY_UTILS_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

namespace il_elem_array_utils {

using value_type = ap_uint<32>;
static constexpr int value_bitwidth = 32;

template<int>
struct unsupported_word_bw { static constexpr bool value = false; };

template<int word_bw>
static constexpr int get_nwords(int len) {
    return (len <= 0) ? 0 : ((len * value_bitwidth + word_bw - 1) / word_bw);
}

template<int word_bw>
static constexpr int pf() {
    return word_bw / 32;
}

// lane_capacity = max(1, pf): the lane-buffer size and loop step (call it LW). It is pf
// in the vectorized regime (pf >= 1) and 1 in the wide-element regime (pf == 0).
template<int word_bw>
static constexpr int lane_capacity() {
    return pf<word_bw>() >= 1 ? pf<word_bw>() : 1;
}

template<int word_bw>
struct read_array_elem_impl {
    static void run(const ap_uint<word_bw>* src, value_type* out, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_array_elem");
        (void)src;
        (void)out;
        (void)n;
    }
};

template<>
struct read_array_elem_impl<64> {
    static value_type run_lane(const ap_uint<64>& w, int k) {
        #pragma HLS INLINE
        switch (k) {
            case 0: return (ap_uint<32>)(w.range(31, 0));
            case 1: return (ap_uint<32>)(w.range(63, 32));
        }
        return value_type();
    }
    static void run(const ap_uint<64>* src, value_type* out, int n) {
        #pragma HLS INLINE
        if (src == nullptr) {
            return;
        }
        ap_uint<64> w = src[0];
        if (n > 0) {
            out[0] = run_lane(w, 0);
        }
        if (n > 1) {
            out[1] = run_lane(w, 1);
        }
    }
};

template<int word_bw>
struct write_array_elem_impl {
    static void run(const value_type* in, ap_uint<word_bw>* dst, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_array_elem");
        (void)in;
        (void)dst;
        (void)n;
    }
};

template<>
struct write_array_elem_impl<64> {
    static void write_lane(ap_uint<64>& w, int k, const value_type& v) {
        #pragma HLS INLINE
        switch (k) {
            case 0: w.range(31, 0) = v; break;
            case 1: w.range(63, 32) = v; break;
        }
    }
    static void run(const value_type* in, ap_uint<64>* dst, int n) {
        #pragma HLS INLINE
        if (dst == nullptr) {
            return;
        }
        ap_uint<64> w = 0;
        if (n > 0) {
            write_lane(w, 0, in[0]);
        }
        if (n > 1) {
            write_lane(w, 1, in[1]);
        }
        dst[0] = w;
    }
};

template<int word_bw>
struct read_stream_elem_impl {
    static void run(hls::stream<ap_uint<word_bw>>& s, value_type* out, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_stream_elem");
        (void)s;
        (void)out;
        (void)n;
    }
};

template<>
struct read_stream_elem_impl<64> {
    static void run(hls::stream<ap_uint<64>>& s, value_type* out, int n) {
        #pragma HLS INLINE
        ap_uint<64> w = s.read();
        if (n > 0) {
            out[0] = (ap_uint<32>)(w.range(31, 0));
        }
        if (n > 1) {
            out[1] = (ap_uint<32>)(w.range(63, 32));
        }
    }
};

template<int word_bw>
struct read_axi4_stream_elem_impl {
    static void run(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* out, streamutils::tlast_status& tl, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_axi4_stream_elem");
        (void)s;
        (void)out;
        (void)tl;
        (void)n;
    }
};

template<>
struct read_axi4_stream_elem_impl<64> {
    static void run(hls::stream<streamutils::axi4s_word<64>>& s, value_type* out, streamutils::tlast_status& tl, int n) {
        #pragma HLS INLINE
        tl = streamutils::tlast_status::no_tlast;
        auto axis_word = s.read();
        ap_uint<64> w = axis_word.data;
        if (n > 0) {
            out[0] = (ap_uint<32>)(w.range(31, 0));
        }
        if (n > 1) {
            out[1] = (ap_uint<32>)(w.range(63, 32));
        }
        if (axis_word.last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }
};

template<int word_bw>
struct write_stream_elem_impl {
    static void run(hls::stream<ap_uint<word_bw>>& s, const value_type* in, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_stream_elem");
        (void)s;
        (void)in;
        (void)n;
    }
};

template<>
struct write_stream_elem_impl<64> {
    static void run(hls::stream<ap_uint<64>>& s, const value_type* in, int n) {
        #pragma HLS INLINE
        ap_uint<64> w = 0;
        if (n > 0) {
            w.range(31, 0) = in[0];
        }
        if (n > 1) {
            w.range(63, 32) = in[1];
        }
        s.write(w);
    }
};

template<int word_bw>
struct write_axi4_stream_elem_impl {
    static void run(hls::stream<streamutils::axi4s_word<word_bw>>& s, const value_type* in, bool tlast, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_axi4_stream_elem");
        (void)s;
        (void)in;
        (void)tlast;
        (void)n;
    }
};

template<>
struct write_axi4_stream_elem_impl<64> {
    static void run(hls::stream<streamutils::axi4s_word<64>>& s, const value_type* in, bool tlast, int n) {
        #pragma HLS INLINE
        ap_uint<64> w = 0;
        if (n > 0) {
            w.range(31, 0) = in[0];
        }
        if (n > 1) {
            w.range(63, 32) = in[1];
        }
        streamutils::write_axi4_word<64>(s, w, tlast);
    }
};

template<int word_bw>
struct read_framed_stream_elem_impl {
    static void run(hls::stream<streamutils::framed_word<word_bw>>& s, value_type* out, streamutils::tlast_status& tl, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for read_framed_stream_elem");
        (void)s;
        (void)out;
        (void)tl;
        (void)n;
    }
};

template<>
struct read_framed_stream_elem_impl<64> {
    static void run(hls::stream<streamutils::framed_word<64>>& s, value_type* out, streamutils::tlast_status& tl, int n) {
        #pragma HLS INLINE
        tl = streamutils::tlast_status::no_tlast;
        auto axis_word = s.read();
        ap_uint<64> w = axis_word.data;
        if (n > 0) {
            out[0] = (ap_uint<32>)(w.range(31, 0));
        }
        if (n > 1) {
            out[1] = (ap_uint<32>)(w.range(63, 32));
        }
        if (axis_word.last) {
            tl = streamutils::tlast_status::tlast_at_end;
        }
    }
};

template<int word_bw>
struct write_framed_stream_elem_impl {
    static void run(hls::stream<streamutils::framed_word<word_bw>>& s, const value_type* in, bool tlast, int n) {
        static_assert(unsupported_word_bw<word_bw>::value, "Unsupported word_bw for write_framed_stream_elem");
        (void)s;
        (void)in;
        (void)tlast;
        (void)n;
    }
};

template<>
struct write_framed_stream_elem_impl<64> {
    static void run(hls::stream<streamutils::framed_word<64>>& s, const value_type* in, bool tlast, int n) {
        #pragma HLS INLINE
        ap_uint<64> w = 0;
        if (n > 0) {
            w.range(31, 0) = in[0];
        }
        if (n > 1) {
            w.range(63, 32) = in[1];
        }
        streamutils::write_boundary_word<streamutils::framed_word<64>, 64>(s, w, tlast);
    }
};

// --- lane methods (Phase 1a): move LW = lane_capacity<W>() = max(1, pf) elements ---
// dst is a buffer of length LW; pf >= 1 -> n valid lanes of one word/beat, pf == 0 ->
// one wide element across ceil(elem/W) words/beats (n ignored).  Memory: the caller
// advances the word pointer by get_nwords<W>(LW); streams self-sequence.

template<int word_bw>
inline void read_array_lane(const ap_uint<word_bw>* src, value_type dst[lane_capacity<word_bw>()], int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    read_array_elem_impl<word_bw>::run(src, dst, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void write_array_lane(const value_type src[lane_capacity<word_bw>()], ap_uint<word_bw>* dst, int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    write_array_elem_impl<word_bw>::run(src, dst, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void read_stream_lane(hls::stream<ap_uint<word_bw>>& s, value_type dst[lane_capacity<word_bw>()], int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    read_stream_elem_impl<word_bw>::run(s, dst, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void write_stream_lane(const value_type src[lane_capacity<word_bw>()], hls::stream<ap_uint<word_bw>>& s, int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    write_stream_elem_impl<word_bw>::run(s, src, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void read_axi4_stream_lane(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type dst[lane_capacity<word_bw>()], int n, streamutils::tlast_status& tl) {
    #pragma HLS INLINE
    read_axi4_stream_elem_impl<word_bw>::run(s, dst, tl, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void read_axi4_stream_lane(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type dst[lane_capacity<word_bw>()], int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
    read_axi4_stream_lane<word_bw>(s, dst, n, tl);
}

template<int word_bw>
inline void write_axi4_stream_lane(const value_type src[lane_capacity<word_bw>()], hls::stream<streamutils::axi4s_word<word_bw>>& s, bool tlast = false, int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    write_axi4_stream_elem_impl<word_bw>::run(s, src, tlast, pf<word_bw>() >= 1 ? n : 1);
}

// --- framed_word lane methods: the same LW-element move over an internal framed_word{data,last}
// beat (no keep/strb sidebands), for composite-internal edges.  Delegates to the framed impls
// below, generated by renaming the axi4 impls (framed_word is field-identical to axi4s_word).
template<int word_bw>
inline void read_framed_stream_lane(hls::stream<streamutils::framed_word<word_bw>>& s, value_type dst[lane_capacity<word_bw>()], int n, streamutils::tlast_status& tl) {
    #pragma HLS INLINE
    read_framed_stream_elem_impl<word_bw>::run(s, dst, tl, pf<word_bw>() >= 1 ? n : 1);
}

template<int word_bw>
inline void read_framed_stream_lane(hls::stream<streamutils::framed_word<word_bw>>& s, value_type dst[lane_capacity<word_bw>()], int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
    read_framed_stream_lane<word_bw>(s, dst, n, tl);
}

template<int word_bw>
inline void write_framed_stream_lane(const value_type src[lane_capacity<word_bw>()], hls::stream<streamutils::framed_word<word_bw>>& s, bool tlast = false, int n = lane_capacity<word_bw>()) {
    #pragma HLS INLINE
    write_framed_stream_elem_impl<word_bw>::run(s, src, tlast, pf<word_bw>() >= 1 ? n : 1);
}

// --- element random access (Phase 3): read/write ONE packed element by index i ---
// iw = i / LW, k = i % LW  (LW = lane_capacity<W>(), compile-time; a power-of-two LW is a
// shift/mask).  Reuses the shared run_lane / write_lane (the single packing-contract source)
// -- the word-granular random-access gather/scatter primitive Phase 4's Gather consumes.
// Requires pf >= 1 (element fits in one word); a wide element (pf == 0) is not supported.
template<int word_bw>
inline value_type elem_read(const ap_uint<word_bw>* src, int i) {
    #pragma HLS INLINE
    static_assert(pf<word_bw>() >= 1, "elem_read requires pf>=1 (element fits in one word)");
    return read_array_elem_impl<word_bw>::run_lane(src[i / lane_capacity<word_bw>()], i % lane_capacity<word_bw>());
}

template<int word_bw>
inline void elem_write(const value_type& v, ap_uint<word_bw>* dst, int i) {
    #pragma HLS INLINE
    static_assert(pf<word_bw>() >= 1, "elem_write requires pf>=1 (element fits in one word)");
    // Specialization: when pf == 1 (element fills the whole word), skip RMW and write directly.
    // When pf > 1 (multiple lanes per word), fall back to lane read-modify-write.
    if constexpr (pf<word_bw>() == 1) {
        // Fast path: element == word width. Direct write, no RMW.
        dst[i] = v;
    } else {
        // Slow path: multiple lanes per word. RMW to update one lane.
        const int iw = i / lane_capacity<word_bw>();
        ap_uint<word_bw> w = dst[iw];
        write_array_elem_impl<word_bw>::write_lane(w, i % lane_capacity<word_bw>(), v);
        dst[iw] = w;
    }
}

// --- range methods (Phase 1b): element-indexed [i0, i1) over memory, on the lane methods ---
// Word-aligned layout: element i is lane (i % LW) of group (i / LW); a group is one word
// (pf >= 1) or ceil(elem/W) words (pf == 0, one wide element). Walk groups with a running
// word pointer (advance WPU = get_nwords<W>(LW) per group) + a per-group lane index -- no
// per-element divide. Element coordinates throughout: the caller never computes i0/PF.

// Regime tag: lane_capacity<W>() selects the slice form by overload resolution (the
// word_bw_tag idiom, keyed on LW) -- no if constexpr. slice_lane_tag<1> (scalar pf==1 or
// wide-element pf==0: one element per WPU contiguous words) takes the flat fixed-trip affine
// path the burst analyzer lowers to a single fixed-length burst; the generic
// slice_lane_tag<lw> (vectorized, lw lanes per word) keeps the group walk.
template<int lw>
struct slice_lane_tag {};

// read, generic (LW > 1): boundary-peel. The aligned middle groups are a pipelined flat-
// affine burst read (one packed word per iteration, the burst analyzer streams it at one
// word/cycle); the <= 1 partial head/tail group extracts only its in-range lanes with a
// fixed LW-trip unrolled predicate.  (A single variable-trip group walk left the middle
// read unpipelined -- each wide read paid full memory latency and the DATAFLOW stage stalled;
// runtime-bounded head/tail loops also synthesize a 2^30 worst-case trip that poisons the
// enclosing DATAFLOW interval.)
template<int word_bw, int lw>
inline void read_array_slice_dispatch(slice_lane_tag<lw>, const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {
    #pragma HLS INLINE
    constexpr int LW = lane_capacity<word_bw>();
    constexpr int WPU = get_nwords<word_bw>(LW);
    const int a0 = ((i0 + LW - 1) / LW) * LW;       // first fully-covered group base (>= i0)
    const int a1 = (i1 / LW) * LW;                  // end of fully-covered groups (<= i1)
    // head partial group [i0, min(a0, i1)): keep only in-range lanes.
    if (a0 > i0) {
        const int gb = (i0 / LW) * LW;
        value_type lane[LW];
        read_array_lane<word_bw>(words + (gb / LW) * WPU, lane, LW);
        for (int l = 0; l < LW; ++l) {
            #pragma HLS UNROLL
            const int e = gb + l;
            if (e >= i0 && e < i1) out[e - i0] = lane[l];
        }
    }
    // aligned middle groups [a0, a1): pipelined flat-affine burst read.
    if (a1 > a0) {
        const ap_uint<word_bw>* wp = words + (a0 / LW) * WPU;
        const int ng = (a1 - a0) / LW;
        const int obase = a0 - i0;
        for (int g = 0; g < ng; ++g) {
            #pragma HLS PIPELINE II=1
            value_type lane[LW];
            read_array_lane<word_bw>(wp + g * WPU, lane, LW);
            for (int l = 0; l < LW; ++l) {
                #pragma HLS UNROLL
                out[obase + g * LW + l] = lane[l];
            }
        }
    }
    // tail partial group [a1, i1): keep only in-range lanes (only when a1 >= i0).
    if (a1 < i1 && a1 >= i0) {
        value_type lane[LW];
        read_array_lane<word_bw>(words + (a1 / LW) * WPU, lane, LW);
        for (int l = 0; l < LW; ++l) {
            #pragma HLS UNROLL
            if (a1 + l < i1) out[a1 + l - i0] = lane[l];
        }
    }
}

// read, scalar (LW == 1): flat fixed-trip affine burst (one element per WPU contiguous words).
template<int word_bw>
inline void read_array_slice_dispatch(slice_lane_tag<1>, const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {
    #pragma HLS INLINE
    constexpr int WPU = get_nwords<word_bw>(1);
    const ap_uint<word_bw>* wp = words + i0 * WPU;
    const int n = i1 - i0;
    for (int e = 0; e < n; ++e) {
        #pragma HLS PIPELINE II=1
        read_array_lane<word_bw>(wp + e * WPU, out + e, 1);
    }
}

template<int word_bw>
inline void read_array_slice(const ap_uint<word_bw>* words, int i0, int i1, value_type* out) {
    #pragma HLS INLINE
    if (words == nullptr || out == nullptr || i1 <= i0) {
        return;
    }
    read_array_slice_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{}, words, i0, i1, out);
}

// write, generic (LW > 1): pure-write. Writes whole words covering [i0, i1); the partial tail
// lane past i1 (up to the word boundary) is clobbered -- safe because Waveflow arrays are word-
// granular (MemMgr alloc rounds to whole words, so that lane is nobody's data). No RMW read on
// the memory bundle: the burst analyzer emits a single write burst, AND -- because the store
// never reads memory -- a load/compute/store DATAFLOW keeps its ping-pong overlap (an RMW read
// makes Vitis serialize load(j+1) behind store(j) on a shared port). REQUIRES i0 word-aligned;
// an unaligned head shares its word with the array's own earlier elements -- use
// write_array_slice_rmw for an unaligned / word-shared sub-range.
template<int word_bw, int lw>
inline void write_array_slice_dispatch(slice_lane_tag<lw>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    constexpr int LW = lane_capacity<word_bw>();
    constexpr int WPU = get_nwords<word_bw>(LW);
    ap_uint<word_bw>* wp = words + (i0 / LW) * WPU;   // i0 word-aligned -> exact group base
    const int n = i1 - i0;
    const int ng = (n + LW - 1) / LW;                 // whole words to cover n (tail padded)
    for (int g = 0; g < ng; ++g) {
        #pragma HLS PIPELINE II=1
        value_type lane[LW];
        for (int l = 0; l < LW; ++l) {
            #pragma HLS UNROLL
            const int e = g * LW + l;
            const int idx = (e < n) ? e : 0;             // in-bounds index (value muxed below)
            lane[l] = (e < n) ? in[idx] : value_type();  // zero-pad the tail lane (matches golden)
        }
        write_array_lane<word_bw>(lane, wp + g * WPU, LW);
    }
}

// write, scalar (LW == 1): pure-write flat affine burst -- no RMW read on the gmem bundle
// (the unconditional RMW read is what forced the write to II=16).
template<int word_bw>
inline void write_array_slice_dispatch(slice_lane_tag<1>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    constexpr int WPU = get_nwords<word_bw>(1);
    ap_uint<word_bw>* wp = words + i0 * WPU;
    const int n = i1 - i0;
    for (int e = 0; e < n; ++e) {
        #pragma HLS PIPELINE II=1
        write_array_lane<word_bw>(in + e, wp + e * WPU, 1);
    }
}

template<int word_bw>
inline void write_array_slice(const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    if (words == nullptr || in == nullptr || i1 <= i0) {
        return;
    }
    write_array_slice_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{}, in, words, i0, i1);
}

// write RMW, generic (LW > 1): boundary-peel read-modify-write for the RARE case of writing a
// sub-range whose boundary word is shared with data to preserve (an unaligned i0, or
// interleaved partial writes into one array). Prefer write_array_slice (pure-write) for word-
// granular arrays -- the RMW read here makes a load/compute/store DATAFLOW serialize on a
// shared memory port. Aligned middle groups are a read-free pure-write burst; the <=1 partial
// head/tail group is RMW with a fixed LW-trip unrolled predicate (a runtime-bounded loop would
// synthesize a 2^30 worst-case trip).
template<int word_bw, int lw>
inline void write_array_slice_rmw_dispatch(slice_lane_tag<lw>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    constexpr int LW = lane_capacity<word_bw>();
    constexpr int WPU = get_nwords<word_bw>(LW);
    const int a0 = ((i0 + LW - 1) / LW) * LW;       // first fully-covered group base (>= i0)
    const int a1 = (i1 / LW) * LW;                  // end of fully-covered groups (<= i1)
    // head partial group [i0, min(a0, i1)): RMW, writing only in-range lanes.
    if (a0 > i0) {
        const int gb = (i0 / LW) * LW;
        ap_uint<word_bw>* wp = words + (gb / LW) * WPU;
        value_type lane[LW];
        read_array_lane<word_bw>(wp, lane, LW);
        for (int l = 0; l < LW; ++l) {
            #pragma HLS UNROLL
            const int e = gb + l;
            if (e >= i0 && e < i1) lane[l] = in[e - i0];
        }
        write_array_lane<word_bw>(lane, wp, LW);
    }
    // aligned middle groups [a0, a1): read-free pure-write burst (one packed word each).
    if (a1 > a0) {
        ap_uint<word_bw>* wp = words + (a0 / LW) * WPU;
        const int ng = (a1 - a0) / LW;
        const int obase = a0 - i0;
        for (int g = 0; g < ng; ++g) {
            #pragma HLS PIPELINE II=1
            value_type lane[LW];
            for (int l = 0; l < LW; ++l) {
                #pragma HLS UNROLL
                lane[l] = in[obase + g * LW + l];
            }
            write_array_lane<word_bw>(lane, wp + g * WPU, LW);
        }
    }
    // tail partial group [a1, i1): RMW, writing only in-range lanes. Only when a1 is an
    // interior boundary the head didn't cover (a1 >= i0).
    if (a1 < i1 && a1 >= i0) {
        ap_uint<word_bw>* wp = words + (a1 / LW) * WPU;
        value_type lane[LW];
        read_array_lane<word_bw>(wp, lane, LW);
        for (int l = 0; l < LW; ++l) {
            #pragma HLS UNROLL
            if (a1 + l < i1) lane[l] = in[a1 + l - i0];
        }
        write_array_lane<word_bw>(lane, wp, LW);
    }
}

// write RMW, scalar (LW == 1): no partial words exist (one element per WPU words), so RMW
// reduces to the pure-write flat affine burst.
template<int word_bw>
inline void write_array_slice_rmw_dispatch(slice_lane_tag<1>, const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    constexpr int WPU = get_nwords<word_bw>(1);
    ap_uint<word_bw>* wp = words + i0 * WPU;
    const int n = i1 - i0;
    for (int e = 0; e < n; ++e) {
        #pragma HLS PIPELINE II=1
        write_array_lane<word_bw>(in + e, wp + e * WPU, 1);
    }
}

template<int word_bw>
inline void write_array_slice_rmw(const value_type* in, ap_uint<word_bw>* words, int i0, int i1) {
    #pragma HLS INLINE
    if (words == nullptr || in == nullptr || i1 <= i0) {
        return;
    }
    write_array_slice_rmw_dispatch(slice_lane_tag<lane_capacity<word_bw>()>{}, in, words, i0, i1);
}

// Whole-array overloads (range [0, N)); N is deduced from the statically-sized buffer.
template<int word_bw, int N>
inline void read_array_slice(const ap_uint<word_bw>* words, value_type (&out)[N]) {
    #pragma HLS INLINE
    read_array_slice<word_bw>(words, 0, N, out);
}

template<int word_bw, int N>
inline void write_array_slice(const value_type (&in)[N], ap_uint<word_bw>* words) {
    #pragma HLS INLINE
    write_array_slice<word_bw>(in, words, 0, N);
}

template<int word_bw>
inline void read_stream(hls::stream<ap_uint<word_bw>>& s, value_type* dst, int len) {
    #pragma HLS INLINE
    if (dst == nullptr || len <= 0) {
        return;
    }
    for (int i = 0; i < len; i += pf<word_bw>()) {
        read_stream_elem_impl<word_bw>::run(s, dst + i, len - i);
    }
}

template<int word_bw>
inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int& nread, int len) {
    #pragma HLS INLINE
    tl = streamutils::tlast_status::no_tlast;
    nread = 0;
    if (dst == nullptr || len <= 0) {
        return;
    }
    bool stop = false;
    for (int i = 0; i < len && !stop; i += pf<word_bw>()) {
        streamutils::tlast_status lane_tl = streamutils::tlast_status::no_tlast;
        const int lane_count = ((len - i) < pf<word_bw>()) ? (len - i) : pf<word_bw>();
        read_axi4_stream_elem_impl<word_bw>::run(s, dst + i, lane_tl, len - i);
        if (lane_tl == streamutils::tlast_status::tlast_early) {
            tl = lane_tl;
            stop = true;
        }
        if (lane_tl != streamutils::tlast_status::tlast_early) {
            nread += lane_count;
        }
        if (lane_tl == streamutils::tlast_status::tlast_at_end) {
            tl = (i + pf<word_bw>() >= len) ? streamutils::tlast_status::tlast_at_end : streamutils::tlast_status::tlast_early;
            stop = true;
        }
    }
}

template<int word_bw>
inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int len) {
    #pragma HLS INLINE
    int nread = 0;
    read_axi4_stream<word_bw>(s, dst, tl, nread, len);
}

template<int word_bw>
inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int& nread, int len) {
    #pragma HLS INLINE
    streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
    read_axi4_stream<word_bw>(s, dst, tl, nread, len);
    }

template<int word_bw>
inline void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, value_type* dst, int len) {
    #pragma HLS INLINE
    streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
    int nread = 0;
    read_axi4_stream<word_bw>(s, dst, tl, nread, len);
    }

template<int word_bw>
inline void write_stream(hls::stream<ap_uint<word_bw>>& s, const value_type* src, int len) {
    #pragma HLS INLINE
    if (src == nullptr || len <= 0) {
        return;
    }
    for (int i = 0; i < len; i += pf<word_bw>()) {
        write_stream_elem_impl<word_bw>::run(s, src + i, len - i);
    }
}

template<int word_bw>
inline void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s, const value_type* src, bool tlast = true, int len = pf<word_bw>()) {
    #pragma HLS INLINE
    if (src == nullptr || len <= 0) {
        return;
    }
    for (int i = 0; i < len; i += pf<word_bw>()) {
        const bool lane_tlast = (i + pf<word_bw>() >= len) ? tlast : false;
        write_axi4_stream_elem_impl<word_bw>::run(s, src + i, lane_tlast, len - i);
    }
}

// --- framed_word bulk: LEN elements over an internal framed_word stream (length known; the
// consumer knows how many to read, so no tlast early-stop -- tl just captures the final beat).
template<int word_bw>
inline void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, value_type* dst, streamutils::tlast_status& tl, int len) {
    #pragma HLS INLINE
    tl = streamutils::tlast_status::no_tlast;
    if (dst == nullptr || len <= 0) {
        return;
    }
    for (int i = 0; i < len; i += pf<word_bw>()) {
        streamutils::tlast_status lane_tl = streamutils::tlast_status::no_tlast;
        read_framed_stream_elem_impl<word_bw>::run(s, dst + i, lane_tl, len - i);
        if (lane_tl == streamutils::tlast_status::tlast_at_end) {
            tl = lane_tl;
        }
    }
}

template<int word_bw>
inline void read_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, value_type* dst, int len) {
    #pragma HLS INLINE
    streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
    read_framed_stream<word_bw>(s, dst, tl, len);
}

template<int word_bw>
inline void write_framed_stream(hls::stream<streamutils::framed_word<word_bw>>& s, const value_type* src, bool tlast = true, int len = pf<word_bw>()) {
    #pragma HLS INLINE
    if (src == nullptr || len <= 0) {
        return;
    }
    for (int i = 0; i < len; i += pf<word_bw>()) {
        const bool lane_tlast = (i + pf<word_bw>() >= len) ? tlast : false;
        write_framed_stream_elem_impl<word_bw>::run(s, src + i, lane_tlast, len - i);
    }
}

}  // namespace il_elem_array_utils

#endif // INCLUDE_IL_ELEM_ARRAY_UTILS_H
