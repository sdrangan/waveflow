"""Patch 3: ping-pong the digit-reversal buffer (cache and write-back overlap across frames)."""
import pathlib

p = pathlib.Path(__file__).parent / "lib/vitis_fft/hls_ssr_fft_data_reorder.hpp"
s = p.read_text()
old = """void digitReversedDataReOrder(hls::stream<std::complex<T_in> > p_inData[t_R],
                              hls::stream<std::complex<T_out> > p_outData[t_R]) {
    //#pragma HLS INLINE
"""
new = """void digitReversedDataReOrder(hls::stream<std::complex<T_in> > p_inData[t_R],
                              hls::stream<std::complex<T_out> > p_outData[t_R]) {
    // WAVEFLOW PATCH: a dataflow region makes digitReverseBuff a ping-pong pair, so frame k is
    // written back while frame k+1 is cached (was: cache, then write back, 2 L/R per frame).
#pragma HLS DATAFLOW
"""
assert s.count(old) == 1
p.write_text(s.replace(old, new))
print("patched digitReversedDataReOrder")
