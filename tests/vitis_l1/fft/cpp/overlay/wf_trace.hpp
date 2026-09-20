// wf_trace.hpp -- per-stage tracing for the DEBUG COPY of the Vitis FFT headers.
//
// Only compiled when -DWF_FFT_TRACE is set, so the vendored headers stay byte-compatible with
// the originals otherwise.  Records raw stored bits plus the declared (W, I) of each value, so
// the Python model can be compared stage by stage rather than only end to end.
#ifndef WF_TRACE_HPP_
#define WF_TRACE_HPP_
#ifdef WF_FFT_TRACE
#include <cstdio>
#include <string>
#include <vector>

struct WfTraceRec {
    std::string tag;
    int idx, W, I;
    long long re, im;
};
inline std::vector<WfTraceRec>& wf_trace_log() {
    static std::vector<WfTraceRec> v;
    return v;
}
template <typename T_cplx>
inline void wf_trace(const char* tag, int idx, const T_cplx& v) {
    typedef typename T_cplx::value_type T_inner;
    WfTraceRec r;
    r.tag = tag; r.idx = idx;
    r.W = T_inner::width; r.I = T_inner::iwidth;
    r.re = (long long)v.real().range().to_int64();
    r.im = (long long)v.imag().range().to_int64();
    wf_trace_log().push_back(r);
}
inline void wf_trace_clear() { wf_trace_log().clear(); }
inline void wf_trace_dump(FILE* f) {
    const std::vector<WfTraceRec>& L = wf_trace_log();
    for (size_t n = 0; n < L.size(); n++)
        fprintf(f, "    {\"seq\": %zu, \"tag\": \"%s\", \"idx\": %d, \"W\": %d, \"I\": %d,"
                   " \"re\": %lld, \"im\": %lld}%s\n",
                n, L[n].tag.c_str(), L[n].idx, L[n].W, L[n].I, L[n].re, L[n].im,
                n + 1 == L.size() ? "" : ",");
}
#define WF_TRACE(tag, idx, v) wf_trace((tag), (idx), (v))
#else
#define WF_TRACE(tag, idx, v) do {} while (0)
#endif
#endif
