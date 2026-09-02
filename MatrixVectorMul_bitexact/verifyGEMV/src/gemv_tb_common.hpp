// gemv_tb_common.hpp -- the bits every testbench here repeats.
//
// Values are read and written as RAW BIT PATTERNS, never decimals.  This kernel's whole
// difficulty is 1-ULP differences from summation order; a "%.17g" round-trip absorbs exactly
// those and would make the comparison meaningless.
#ifndef GEMV_TB_COMMON_HPP_
#define GEMV_TB_COMMON_HPP_

#include <cstdio>
#include <cstring>

static inline float f_of(unsigned int b) { float f; memcpy(&f, &b, 4); return f; }
static inline unsigned int b_of(float f) { unsigned int b; memcpy(&b, &f, 4); return b; }

// Skip the leading '#' comment block of an input file.
static inline void skip_comments(FILE* fi) {
    int c;
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
}

#endif
