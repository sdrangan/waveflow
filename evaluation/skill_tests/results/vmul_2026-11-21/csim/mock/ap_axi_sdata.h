#pragma once
#include <ap_int.h>
template <int D, int U, int I, int De> struct ap_axis {
    ap_uint<D> data; ap_uint<(D + 7) / 8> keep; ap_uint<(D + 7) / 8> strb; ap_uint<1> last;
};
