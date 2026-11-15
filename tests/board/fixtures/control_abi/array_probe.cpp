// Standalone S0c ABI fixture: AXI-Lite BRAM-backed array region.
void array_probe(unsigned values[8], unsigned &sum) {
#pragma HLS interface s_axilite port=values bundle=control
#pragma HLS interface s_axilite port=sum bundle=control
#pragma HLS interface s_axilite port=return bundle=control
    unsigned acc = 0;
    for (unsigned i = 0; i < 8; ++i) acc += values[i];
    sum = acc;
}
