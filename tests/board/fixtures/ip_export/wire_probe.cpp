// No AXI-Lite control interface: no control.h should be required by the exporter.
void wire_probe(unsigned x, unsigned &y) {
#pragma HLS interface ap_ctrl_none port=return
#pragma HLS interface ap_none port=x
#pragma HLS interface ap_none port=y
    y = x + 1;
}
