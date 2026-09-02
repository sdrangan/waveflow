// gemv_tb_common.hpp -- the file I/O every testbench here repeats.
//
// Values are read and written as RAW BIT PATTERNS, never decimals.  This kernel's whole
// difficulty is 1-ULP differences from summation order; a "%.17g" round-trip absorbs exactly
// those and would make the comparison meaningless.
#ifndef GEMV_TB_COMMON_HPP_
#define GEMV_TB_COMMON_HPP_

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static inline float f_of(unsigned int b) { float f; memcpy(&f, &b, 4); return f; }
static inline unsigned int b_of(float f) { unsigned int b; memcpy(&b, &f, 4); return b; }
static inline double d_of(unsigned long long b) { double d; memcpy(&d, &b, 8); return d; }
static inline unsigned long long b_of(double d) { unsigned long long b; memcpy(&b, &d, 8); return b; }

// Skip the leading '#' comment block of an input file.
static inline void skip_comments(FILE* fi) {
    int c;
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
}

// Read `n_case` blocks of (M*N matrix, N vector) stored as decimal bit patterns, and convert each
// through CONV into the element type.  Used by the float and double testbenches.
template <typename T, typename BITS, T (*CONV)(BITS)>
static bool read_bit_cases(const char* path, int want_m, int want_n, int& n_case,
                           std::vector<std::vector<T> >& As, std::vector<std::vector<T> >& xs) {
    FILE* fi = fopen(path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", path); return false; }
    skip_comments(fi);
    int m, n;
    if (fscanf(fi, "%d %d %d", &n_case, &m, &n) != 3) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", path); fclose(fi); return false;
    }
    if (m != want_m || n != want_n) {
        printf("WAVEFLOW_ERROR: input is M=%d N=%d, DUT is M=%d N=%d\n", m, n, want_m, want_n);
        fclose(fi); return false;
    }
    As.resize(n_case); xs.resize(n_case);
    unsigned long long u;
    for (int k = 0; k < n_case; k++) {
        As[k].resize(m * n); xs[k].resize(n);
        for (int i = 0; i < m * n; i++) {
            if (fscanf(fi, "%llu", &u) != 1) { printf("WAVEFLOW_ERROR: short A\n"); fclose(fi); return false; }
            As[k][i] = CONV((BITS)u);
        }
        for (int j = 0; j < n; j++) {
            if (fscanf(fi, "%llu", &u) != 1) { printf("WAVEFLOW_ERROR: short x\n"); fclose(fi); return false; }
            xs[k][j] = CONV((BITS)u);
        }
    }
    fclose(fi);
    return true;
}

// Read `n_case` blocks of signed decimal integers.  Used by the integer and ap_fixed testbenches,
// where the file holds stored values rather than float bit patterns.
static inline bool read_int_cases(const char* path, int want_m, int want_n, int& n_case,
                                  std::vector<std::vector<long long> >& As,
                                  std::vector<std::vector<long long> >& xs) {
    FILE* fi = fopen(path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", path); return false; }
    skip_comments(fi);
    int m, n;
    if (fscanf(fi, "%d %d %d", &n_case, &m, &n) != 3) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", path); fclose(fi); return false;
    }
    if (m != want_m || n != want_n) {
        printf("WAVEFLOW_ERROR: input is M=%d N=%d, DUT is M=%d N=%d\n", m, n, want_m, want_n);
        fclose(fi); return false;
    }
    As.resize(n_case); xs.resize(n_case);
    for (int k = 0; k < n_case; k++) {
        As[k].resize(m * n); xs[k].resize(n);
        for (int i = 0; i < m * n; i++)
            if (fscanf(fi, "%lld", &As[k][i]) != 1) { printf("WAVEFLOW_ERROR: short A\n"); fclose(fi); return false; }
        for (int j = 0; j < n; j++)
            if (fscanf(fi, "%lld", &xs[k][j]) != 1) { printf("WAVEFLOW_ERROR: short x\n"); fclose(fi); return false; }
    }
    fclose(fi);
    return true;
}

#endif
