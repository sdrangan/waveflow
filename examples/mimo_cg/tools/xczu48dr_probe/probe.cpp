#include <ap_fixed.h>
#include <complex>
typedef ap_fixed<16, 2> fx_t;
typedef ap_fixed<40, 10> acc_t;
// Complex MAC over 8 elements: the inner op of a CG dot product.
void probe_cmac(const std::complex<fx_t> a[8], const std::complex<fx_t> b[8], std::complex<acc_t>* y) {
#pragma HLS PIPELINE II=1
    acc_t re = 0, im = 0;
    for (int i = 0; i < 8; i++) {
        re += a[i].real() * b[i].real() - a[i].imag() * b[i].imag();
        im += a[i].real() * b[i].imag() + a[i].imag() * b[i].real();
    }
    *y = std::complex<acc_t>(re, im);
}
