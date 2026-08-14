// The wrapper: the kernel plus its memory, as one elaboratable design.
//
// This is the artifact the whole experiment is about.  The kernel cannot contain the memory (Vitis
// turns a shared local array into a synchronizing PIPO channel, and refuses a single port used both
// ways), so the memory lives beside it and the wrapper is what joins them.  From outside, this looks
// like a kernel with only its AXIS ports -- the BRAM is internal and invisible to any testbench.
//
// It is also the DESIGN SCOPE: what is inside this module is what a resource estimate should count,
// and the memory is inside it.  csynth of the kernel alone never sees it.
`timescale 1ns/1ps
module rx_top (
    input         ap_clk,
    input         ap_rst_n,
    input  [15:0] rx_str_TDATA,   input  rx_str_TVALID,   output rx_str_TREADY,
    input  [15:0] addr_str_TDATA, input  addr_str_TVALID, output addr_str_TREADY,
    output [15:0] out_str_TDATA,  output out_str_TVALID,  input  out_str_TREADY
);
    wire [31:0] bw_addr_a, bw_addr_b, br_addr_a, br_addr_b;
    wire        bw_en_a,   bw_en_b,   br_en_a,   br_en_b;
    wire [15:0] bw_din_a,  bw_din_b,  br_din_a,  br_din_b;
    wire [15:0] bw_dout_a, bw_dout_b, br_dout_a, br_dout_b;
    wire [1:0]  bw_we_a,   bw_we_b,   br_we_a,   br_we_b;

    rx kernel (
        .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
        .rx_str_TDATA(rx_str_TDATA),   .rx_str_TVALID(rx_str_TVALID),   .rx_str_TREADY(rx_str_TREADY),
        .addr_str_TDATA(addr_str_TDATA), .addr_str_TVALID(addr_str_TVALID), .addr_str_TREADY(addr_str_TREADY),
        .out_str_TDATA(out_str_TDATA), .out_str_TVALID(out_str_TVALID), .out_str_TREADY(out_str_TREADY),
        .buf_w_Addr_A(bw_addr_a), .buf_w_EN_A(bw_en_a), .buf_w_Din_A(bw_din_a),
        .buf_w_Dout_A(bw_dout_a), .buf_w_WEN_A(bw_we_a), .buf_w_Clk_A(), .buf_w_Rst_A(),
        .buf_w_Addr_B(bw_addr_b), .buf_w_EN_B(bw_en_b), .buf_w_Din_B(bw_din_b),
        .buf_w_Dout_B(bw_dout_b), .buf_w_WEN_B(bw_we_b), .buf_w_Clk_B(), .buf_w_Rst_B(),
        .buf_r_Addr_A(br_addr_a), .buf_r_EN_A(br_en_a), .buf_r_Din_A(br_din_a),
        .buf_r_Dout_A(br_dout_a), .buf_r_WEN_A(br_we_a), .buf_r_Clk_A(), .buf_r_Rst_A(),
        .buf_r_Addr_B(br_addr_b), .buf_r_EN_B(br_en_b), .buf_r_Din_B(br_din_b),
        .buf_r_Dout_B(br_dout_b), .buf_r_WEN_B(br_we_b), .buf_r_Clk_B(), .buf_r_Rst_B()
    );

    // ONE memory, two logical ports: buf_w drives port A, buf_r drives port B.  Each of the
    // kernel's two interfaces emits an A/B pair; only the A half of each is used here, which is the
    // wiring question the experiment exists to settle.
    bram_t2p #(.DW(16), .AW(10)) mem (
        .clk(ap_clk),
        .a_addr(bw_addr_a), .a_en(bw_en_a), .a_din(bw_din_a), .a_we(bw_we_a), .a_dout(bw_dout_a),
        .b_addr(br_addr_a), .b_en(br_en_a), .b_din(br_din_a), .b_we(br_we_a), .b_dout(br_dout_a)
    );
    assign bw_dout_b = 16'd0;
    assign br_dout_b = 16'd0;
endmodule
