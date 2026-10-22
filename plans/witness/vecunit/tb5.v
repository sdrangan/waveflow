// tb5.v -- witness 5 at RTL (xsim), where Vitis co-simulation cannot judge: its C phase mis-models
// this free-running three-task design even with the documented scoped locks (w5d). Drives w5_top's
// ap_fifo ports directly: 256 lane groups on in_r, two jobs' programs on prog; collects out_r and
// status and checks them -- every input lane group plus one, in order, and the tx_ids 50, 51.
`timescale 1ns / 1ps
module tb;
    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    // in_r: 4 blocks of 64 lane groups, value 1000*b + i
    reg  [95:0] in_mem [0:255];
    integer in_idx = 0;
    wire [95:0] in_r_dout = in_mem[in_idx];
    wire in_r_empty_n = (in_idx < 256);
    wire in_r_read;

    // prog: per job, the header (tx_id, n = 20) then per block 8 COPY g, POP, PUSH
    reg  [63:0] prog_mem [0:63];
    integer prog_idx = 0, prog_n = 0;
    wire [63:0] prog_dout = prog_mem[prog_idx];
    wire prog_empty_n = (prog_idx < prog_n);
    wire prog_read;

    wire [95:0] out_r_din;
    wire out_r_write;
    wire [63:0] status_din;
    wire status_write;
    integer nout = 0, nst = 0, bad = 0, cyc = 0, last_out = 0;

    w5_top dut (.ap_clk(clk), .ap_rst(rst),
        .in_r_dout(in_r_dout), .in_r_empty_n(in_r_empty_n), .in_r_read(in_r_read),
        .prog_dout(prog_dout), .prog_empty_n(prog_empty_n), .prog_read(prog_read),
        .out_r_din(out_r_din), .out_r_full_n(1'b1), .out_r_write(out_r_write),
        .status_din(status_din), .status_full_n(1'b1), .status_write(status_write));

    integer b, i, j, g;
    initial begin
        for (b = 0; b < 4; b = b + 1)
            for (i = 0; i < 64; i = i + 1) in_mem[b * 64 + i] = 1000 * b + i;
        for (j = 0; j < 2; j = j + 1) begin
            prog_mem[prog_n] = {32'd0, 16'd20, 16'd50 + j[15:0]};
            prog_n = prog_n + 1;
            for (b = 0; b < 2; b = b + 1) begin
                for (g = 0; g < 8; g = g + 1) begin
                    prog_mem[prog_n] = {12'd0, g[11:0], 33'd0, 7'd0};   // COPY g (imm1 = g)
                    prog_n = prog_n + 1;
                end
                prog_mem[prog_n] = 64'd1; prog_n = prog_n + 1;          // POP
                prog_mem[prog_n] = 64'd2; prog_n = prog_n + 1;          // PUSH
            end
        end
        repeat (5) @(posedge clk);
        rst <= 0;
    end

    always @(posedge clk) if (!rst) begin
        cyc <= cyc + 1;
        if (in_r_read && in_r_empty_n) in_idx <= in_idx + 1;
        if (prog_read && prog_empty_n) prog_idx <= prog_idx + 1;
        if (out_r_write) begin
            if (out_r_din !== 1000 * (nout / 64) + (nout % 64) + 1) bad <= bad + 1;
            nout <= nout + 1;
            last_out <= cyc;
        end
        if (status_write) begin
            if (status_din !== 50 + nst) bad <= bad + 1;
            nst <= nst + 1;
        end
        if (cyc == 4000) begin
            $display("WITNESS-RTL5 out %0d/256 status %0d/2 mismatches %0d last_out_cycle %0d",
                     nout, nst, bad, last_out);
            if (nout == 256 && nst == 2 && bad == 0) $display("VERDICT_PASS");
            else $display("VERDICT_FAIL");
            $finish;
        end
    end
endmodule
