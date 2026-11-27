// Standalone S0c ABI fixture: implicit 64-bit m_axi offset=slave pointer.
void pointer_probe(unsigned *buffer, unsigned count, unsigned &sum) {
#pragma HLS interface m_axi port=buffer offset=slave bundle=gmem depth=16
#pragma HLS interface s_axilite port=buffer bundle=control
#pragma HLS interface s_axilite port=count bundle=control
#pragma HLS interface s_axilite port=sum bundle=control
#pragma HLS interface s_axilite port=return bundle=control
    unsigned acc = 0;
    for (unsigned i = 0; i < count; ++i) acc += buffer[i];
    sum = acc;
}
