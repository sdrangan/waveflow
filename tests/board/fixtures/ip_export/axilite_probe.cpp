// Standalone S0b framework fixture, deliberately independent of any example.
void axilite_probe(unsigned x, unsigned &y) {
#pragma HLS interface s_axilite port=x bundle=control
#pragma HLS interface s_axilite port=y bundle=control
#pragma HLS interface s_axilite port=return bundle=control
    y = x + 1;
}
