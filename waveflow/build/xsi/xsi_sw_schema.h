#ifndef WAVEFLOW_XSI_SW_SCHEMA_H
#define WAVEFLOW_XSI_SW_SCHEMA_H
// xsi_sw_schema.h — typed messages for a software host (plans/host_runtime.md S3).
//
// A host program reads and writes the same DataSchema structs its kernels use: the generated
// <schema>.h headers (MkvResp, FirStatus, ...), which are HLS-flavored (ap_uint) and compile in host
// code against Vitis's header-only ap_int.h.  Include this header -- after the schema headers -- to use
// the typed endpoint calls of xsi_sw.h:
//
//     MkvResp r = qresp.get<MkvResp>();        // ~ yield from qresp.get_schema(MkvResp)
//     cfg.write(make_cfg(...));               // a struct: serialized as the Python serializer does
//
// The testbench is then compiled with Vitis's include dir and the example's include/ (run.bat /
// run.sh read WF_TB_CXXFLAGS; XsiWorkspace.prepare(tb_include_dirs=...) sets it).
#include <ap_int.h>
#include <cstdint>
#include <vector>

#include "xsi_sw.h"

namespace wfbfm {

template <class T, int BW>
T decode_words(const std::vector<uint64_t>& w) {
    static_assert(BW > 0 && BW <= 64, "typed host messages are BW <= 64 bits per bus word");
    constexpr int n = T::template nwords<BW>();
    if ((int)w.size() != n) {
        std::fprintf(stderr, "FATAL: decode: %zu words for a %d-word message\n", w.size(), n);
        std::exit(5);
    }
    ap_uint<BW> x[n];
    for (int i = 0; i < n; ++i) x[i] = w[i];
    T t;
    t.template read_array<BW>(x);
    return t;
}

template <class T, int BW>
std::vector<uint64_t> encode_words(const T& t) {
    static_assert(BW > 0 && BW <= 64, "typed host messages are BW <= 64 bits per bus word");
    constexpr int n = T::template nwords<BW>();
    ap_uint<BW> x[n];
    t.template write_array<BW>(x);
    std::vector<uint64_t> w(n);
    for (int i = 0; i < n; ++i) w[i] = (uint64_t)x[i];
    return w;
}

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_SW_SCHEMA_H
