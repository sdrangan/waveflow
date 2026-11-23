// A stand-in for the generated tag.h: the tag as its one 128-bit word, which the check only moves.
#pragma once
#include <ap_int.h>
struct Tag {
    ap_uint<128> word;
    template <int W> static constexpr int nwords() { return W >= 48 ? 1 : 2; }
};
