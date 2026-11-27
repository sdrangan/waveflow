// Standalone S0c ABI fixture: signed, floating-point and wide integer scalars.
void scalar_probe(int x, float gain, unsigned long long wide, int &y) {
#pragma HLS interface s_axilite port=x bundle=control
#pragma HLS interface s_axilite port=gain bundle=control
#pragma HLS interface s_axilite port=wide bundle=control
#pragma HLS interface s_axilite port=y bundle=control
#pragma HLS interface s_axilite port=return bundle=control
    y = x + (int)gain + (int)wide;
}
