#ifndef FIR_HPP
#define FIR_HPP
#include <hls_stream.h>
#include <ap_int.h>
#include "fir_dataflow.tpp"
typedef fir_dataflow::real_t real_t;
#include "fir_respond_impl.tpp"
#endif
