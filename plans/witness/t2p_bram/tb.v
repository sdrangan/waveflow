// The ramp test.  Task A writes buf[i] = i+100 for i in 0..255; task B is then asked for a set of
// addresses and must return exactly what A wrote.  If the BRAM latency is wrong, the values come
// back shifted by one and this fails -- which is the whole point of reading a RAMP rather than a
// constant.
`timescale 1ns/1ps
module tb;
    reg clk = 0, rst_n = 0;
    always #5 clk = ~clk;

    reg  [15:0] rx_d = 0, ad_d = 0;
    reg         rx_v = 0, ad_v = 0, out_r = 1;
    wire        rx_rdy, ad_rdy, out_v;
    wire [15:0] out_d;
    integer i, errors = 0;
    reg [15:0] got [0:15];
    integer ngot = 0;

    rx_top dut (.ap_clk(clk), .ap_rst_n(rst_n),
        .rx_str_TDATA(rx_d), .rx_str_TVALID(rx_v), .rx_str_TREADY(rx_rdy),
        .addr_str_TDATA(ad_d), .addr_str_TVALID(ad_v), .addr_str_TREADY(ad_rdy),
        .out_str_TDATA(out_d), .out_str_TVALID(out_v), .out_str_TREADY(out_r));

    // Collect whatever comes out.
    always @(posedge clk) if (out_v && out_r) begin got[ngot] = out_d; ngot = ngot + 1; end

    task push_rx(input [15:0] v);
        begin @(negedge clk); rx_d = v; rx_v = 1; @(posedge clk);
              while (!rx_rdy) @(posedge clk); @(negedge clk); rx_v = 0; end
    endtask
    task push_addr(input [15:0] a);
        begin @(negedge clk); ad_d = a; ad_v = 1; @(posedge clk);
              while (!ad_rdy) @(posedge clk); @(negedge clk); ad_v = 0; end
    endtask

    initial begin
        repeat (10) @(posedge clk); rst_n = 1; repeat (5) @(posedge clk);
        for (i = 0; i < 256; i = i + 1) push_rx(i + 100);      // buf[i] = i+100
        repeat (20) @(posedge clk);
        push_addr(0); push_addr(1); push_addr(7); push_addr(255); push_addr(128);
        repeat (60) @(posedge clk);

        if (ngot != 5) begin
            $display("FAIL: expected 5 words back, got %0d", ngot); errors = errors + 1;
        end else begin
            if (got[0] !== 16'd100) begin $display("FAIL addr0:   %0d != 100",   got[0]); errors=errors+1; end
            if (got[1] !== 16'd101) begin $display("FAIL addr1:   %0d != 101",   got[1]); errors=errors+1; end
            if (got[2] !== 16'd107) begin $display("FAIL addr7:   %0d != 107",   got[2]); errors=errors+1; end
            if (got[3] !== 16'd355) begin $display("FAIL addr255: %0d != 355",   got[3]); errors=errors+1; end
            if (got[4] !== 16'd228) begin $display("FAIL addr128: %0d != 228",   got[4]); errors=errors+1; end
        end
        if (errors == 0) $display("T2P-BRAM EXPERIMENT: PASS (%0d words, ramp verified)", ngot);
        else             $display("T2P-BRAM EXPERIMENT: FAIL (%0d errors)", errors);
        $finish;
    end
endmodule
