// duplex_toy.cpp — isolate whether ONE Vitis m_axi bundle is full- or half-duplex in cosim.
//
// Mirrors the free-running FIR's structure (one `gmem` bundle carrying both reads and writes).
// Three modes, each an II=1 pipelined loop over N beats on the SAME bundle:
//   mode 0  read-only  :  acc += gmem[i]                 -> ~N beats of read
//   mode 1  write-only :  gmem[N+i] = i                  -> ~N beats of write
//   mode 2  read+write :  gmem[N+i] = gmem[i] + 1        -> ONE process: read AND write together
//   mode 3  rw-dataflow:  rd_proc -> fifo -> wr_proc     -> TWO DATAFLOW processes on one bundle
//                                                           (mirrors the FIR's load ∥ store)
// The verdict is from COSIM cycles (the random-stall AXI BFM, the regime the FIR period lives in),
// NOT csynth II:
//   cosim ~= cosim(mode0)              -> FULL-duplex  (read+write overlap; occupancy MAXes)
//   cosim ~= cosim(mode0)+cosim(mode1) -> SERIALIZED   (read+write serialize; occupancy ADDS)
// mode2 (one process) vs mode3 (two processes) isolates whether the serialization the FIR shows is
// inter-PROCESS bus arbitration (mode3 serial, mode2 full) rather than a half-duplex bundle.
#include <ap_int.h>
#include <cstdint>
#include <hls_stream.h>

#define MEM_DW 32

static void rd_proc(ap_uint<MEM_DW>* gmem, int n, hls::stream<ap_uint<MEM_DW> >& f) {
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        f.write(gmem[i]);
    }
}
static void wr_proc(ap_uint<MEM_DW>* gmem, int n, hls::stream<ap_uint<MEM_DW> >& f) {
    for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        gmem[n + i] = f.read() + 1;
    }
}
static void rw_dataflow(ap_uint<MEM_DW>* gmem, int n) {
#pragma HLS DATAFLOW
    hls::stream<ap_uint<MEM_DW> > f("f");
#pragma HLS STREAM variable=f depth=64
    rd_proc(gmem, n, f);
    wr_proc(gmem, n, f);
}

extern "C" void duplex(ap_uint<MEM_DW>* gmem, int n, int mode, ap_uint<MEM_DW>* ret) {
#pragma HLS INTERFACE m_axi port=gmem bundle=gmem offset=slave depth=8192
#pragma HLS INTERFACE m_axi port=ret  bundle=gmem offset=slave depth=4
#pragma HLS INTERFACE s_axilite port=n
#pragma HLS INTERFACE s_axilite port=mode
#pragma HLS INTERFACE s_axilite port=return
    ap_uint<MEM_DW> acc = 0;
    if (mode == 3) {
        rw_dataflow(gmem, n);
    } else if (mode == 0) {
    rd:
        for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
            acc += gmem[i];
        }
        ret[0] = acc;
    } else if (mode == 1) {
    wr:
        for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
            gmem[n + i] = (ap_uint<MEM_DW>)i;
        }
    } else {
    rw:
        for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
            gmem[n + i] = gmem[i] + 1;   // a read AND a write each iteration, same bundle
        }
    }
}
