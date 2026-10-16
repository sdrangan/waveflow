/*
 * Copyright 2019 Xilinx, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file dotHelper.hpp
 * @brief BLAS Level 1 dot helper template function implementation.
 *
 * This file is part of Vitis BLAS Library.
 */

#ifndef XF_BLAS_DOT_HELP_HPP
#define XF_BLAS_DOT_HELP_HPP

// ---------------------------------------------------------------------------------------------
// WF: a copy of blas/L1/include/hw/xf_blas/helpers/funcs/dotHelper.hpp carrying ONE change,
// marked WF-PATCH below.  Included BEFORE <xf_blas.hpp>, its include guard (defined immediately
// above) suppresses the shipped copy -- so nothing in the vendor tree is edited.
//
// Its only purpose is to answer "what would gemv emit if the defect were fixed?".  The
// as-shipped golden comes from the untouched library; this one is clearly labelled everywhere
// it appears so the two are never confused.
// ---------------------------------------------------------------------------------------------

#ifndef __cplusplus
#error "BLAS Library only works with C++."
#endif

#include "ap_int.h"
#include "hls_stream.h"
#include "xf_blas/helpers.hpp"

namespace xf {

namespace blas {

/**
 * @brief dot function that returns the dot product of vector x and y.
 *
 * @tparam t_DataType the data type of the vector entries
 * @tparam t_LogParEntries log2 of the number of parallelly processed entries in the input vector
 * @tparam t_IndexType the datatype of the index
 *
 * @param p_n the number of entries in the input vector p_x, p_n % l_ParEntries == 0
 * @param p_iter the number of repeating iterations
 * @param p_x the input stream of packed vector entries
 * @param p_res the dot product of x and y
 */
namespace {

template <typename t_DataType,
          unsigned int t_LogParEntries,
          typename t_IndexType = unsigned int,
          typename t_MacDataType = t_DataType>
void dot_tree(unsigned int p_n,
              const unsigned int p_iter,
              hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_x,
              hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_y,
              hls::stream<typename WideType<t_MacDataType, 1>::t_TypeInt>& p_res) {
#ifndef __SYNTHESIS__
    assert(p_n % (1 << t_LogParEntries) == 0);
#endif
#pragma HLS DATAFLOW
    hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt> l_mulStr;
    mul<t_DataType, 1 << t_LogParEntries, t_IndexType, t_MacDataType>(p_n, p_x, p_y, l_mulStr, p_iter);
    sum<t_DataType, t_LogParEntries, t_IndexType, t_MacDataType>(p_n, l_mulStr, p_res, p_iter);
}

template <typename t_DataType,
          unsigned int t_LogParEntries,
          typename t_IndexType = unsigned int,
          typename t_MacDataType = t_DataType>
void dot_dsp(unsigned int p_n,
             const unsigned int p_iter,
             hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_x,
             hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_y,
             hls::stream<typename WideType<t_MacDataType, 1>::t_TypeInt>& p_res) {
#ifndef __SYNTHESIS__
    assert(p_n % (1 << t_LogParEntries) == 0);
#endif

    t_MacDataType l_res = 0;
    const unsigned int l_numIter = p_n >> t_LogParEntries;
    const unsigned int l_parEntries = 1 << t_LogParEntries;
    for (t_IndexType l = 0; l < p_iter; ++l) {
        for (t_IndexType i = 0; i < l_numIter; ++i) {
#pragma HLS PIPELINE
            if (i == 0) l_res = 0;
            WideType<t_DataType, l_parEntries> l_x = p_x.read();
#pragma HLS ARRAY_PARTITION variable = l_x complete dim = 1
            WideType<t_DataType, l_parEntries> l_y = p_y.read();
#pragma HLS ARRAY_PARTITION variable = l_y complete dim = 1
            for (t_IndexType j = 0; j < l_parEntries; ++j) {
                l_res += l_x[j] * l_y[j];
            }
            // WF-PATCH (the only change in this file): pack through WideType instead of
            // converting the value.  `p_res.write(l_res)` narrows t_MacDataType to the stream's
            // ap_uint by NUMERIC conversion, which is the identity for integers and destroys the
            // fractional bits of an ap_fixed.  postProcess (the float path, sum.hpp:79) writes a
            // WideType and so packs by bit pattern; this line does not.  See PLAN.md S4.
            if (i == l_numIter - 1) {
                WideType<t_MacDataType, 1> l_out(l_res);
                p_res.write(l_out);
            }
        }
    }
}
} // namespace

template <typename t_DataType,
          unsigned int t_LogParEntries,
          typename t_IndexType = unsigned int,
          typename t_MacDataType = t_DataType>
class DotHelper {
   public:
    static void dot(unsigned int p_n,
                    const unsigned int p_iter,
                    hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_x,
                    hls::stream<typename WideType<t_DataType, 1 << t_LogParEntries>::t_TypeInt>& p_y,
                    hls::stream<typename WideType<t_MacDataType, 1>::t_TypeInt>& p_res) {
#ifndef __SYNTHESIS__
        assert(p_n % (1 << t_LogParEntries) == 0);
#endif
#pragma HLS DATAFLOW
        dot_dsp<t_DataType, t_LogParEntries, t_IndexType, t_MacDataType>(p_n, p_iter, p_x, p_y, p_res);
    }
};
template <unsigned int t_LogParEntries, typename t_IndexType>
class DotHelper<float, t_LogParEntries, t_IndexType, float> {
   public:
    static void dot(const unsigned int p_n,
                    const unsigned int p_iter,
                    hls::stream<typename WideType<float, 1 << t_LogParEntries>::t_TypeInt>& p_x,
                    hls::stream<typename WideType<float, 1 << t_LogParEntries>::t_TypeInt>& p_y,
                    hls::stream<typename WideType<float, 1>::t_TypeInt>& p_res) {
#ifndef __SYNTHESIS__
        assert(p_n % (1 << t_LogParEntries) == 0);
#endif
#pragma HLS DATAFLOW
        dot_tree<float, t_LogParEntries, t_IndexType>(p_n, p_iter, p_x, p_y, p_res);
    }
};

template <unsigned int t_LogParEntries, typename t_IndexType>
class DotHelper<double, t_LogParEntries, t_IndexType, double> {
   public:
    static void dot(const unsigned int p_n,
                    const unsigned int p_iter,
                    hls::stream<typename WideType<double, 1 << t_LogParEntries>::t_TypeInt>& p_x,
                    hls::stream<typename WideType<double, 1 << t_LogParEntries>::t_TypeInt>& p_y,
                    hls::stream<typename WideType<double, 1>::t_TypeInt>& p_res) {
#ifndef __SYNTHESIS__
        assert(p_n % (1 << t_LogParEntries) == 0);
#endif
#pragma HLS DATAFLOW
        dot_tree<double, t_LogParEntries, t_IndexType>(p_n, p_iter, p_x, p_y, p_res);
    }
};

} // end namespace blas

} // end namespace xf

#endif
