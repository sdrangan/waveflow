// mm_bram_port_p1.v -- mm_bram_port.v's BRAM window with a read word every cycle.
//
// The same request-bus leaf onto port A of a block RAM (see mm_bram_port.v), but its read response
// is not registered again: the memory's own output register (bram_t2p, READ_LATENCY 1) is the
// response.  A read's data is on rsp_rdata the cycle after its request, and since axi_slave_front
// issues the next read in the cycle a response is taken, a burst streams one beat a cycle where
// mm_bram_port takes two (LAT + 1).
//
// For a memory that is simulated, not synthesized: the response path is the BRAM's output straight
// to the AXI read channel, which a timing-closed design would register.  Selected by a memory class
// that names this leaf (``PipelinedMemoryMod``, ``BramViewP1``); every existing memory keeps
// mm_bram_port.v.
//
// Holding a response: the leaf issues no new memory access while one is pending (req_ready low for
// both reads and writes), so the BRAM's output register, enabled only by an access, keeps the word
// until rsp_ready.  Only LAT == 1 is supported (bram_t2p's published latency).
`timescale 1ns/1ps
module mm_bram_port_p1 #(
    parameter integer DW  = 64,
    parameter integer LAW = 12,
    parameter integer BAW = 9,      // memory address bits (words)
    parameter integer LAT = 1       // the memory's read latency: must be 1
) (
    input  wire              ap_clk,
    input  wire              ap_rst_n,
    // request bus (from axi_slave_front, or a decoder)
    input  wire              req_valid,
    output wire              req_ready,
    input  wire              req_we,
    input  wire [LAW-1:0]    req_addr,
    input  wire [DW-1:0]     req_wdata,
    output wire              rsp_valid,
    input  wire              rsp_ready,
    output wire [DW-1:0]     rsp_rdata,
    output wire              rsp_err,
    // memory port (bram_t2p port A)
    output wire [31:0]       bram_addr,
    output wire              bram_en,
    output wire [1:0]        bram_we,
    output wire [DW-1:0]     bram_din,
    input  wire [DW-1:0]     bram_dout
);
    localparam integer WB = $clog2(DW / 8);

    wire [LAW-1-WB:0] widx   = req_addr[LAW-1:WB];
    wire              in_mem = (widx < (1 << BAW));

    reg  pend_q;                    // a read's data is on bram_dout, not yet taken
    reg  oob_q;                     // ...and it was outside the memory

    wire take  = pend_q && rsp_ready;
    assign req_ready = !pend_q || take;
    wire wr_hs = req_valid && req_we && req_ready;
    wire rd_hs = req_valid && !req_we && req_ready;

    assign bram_en   = (wr_hs || rd_hs) && in_mem;
    assign bram_we   = (wr_hs && in_mem) ? 2'b11 : 2'b00;
    assign bram_addr = {{(32 - (LAW - WB)){1'b0}}, widx};
    assign bram_din  = req_wdata;

    assign rsp_valid = pend_q;
    assign rsp_rdata = oob_q ? {DW{1'b0}} : bram_dout;
    assign rsp_err   = pend_q && oob_q;

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            pend_q <= 1'b0; oob_q <= 1'b0;
        end else if (rd_hs) begin
            pend_q <= 1'b1; oob_q <= !in_mem;
        end else if (take) begin
            pend_q <= 1'b0;
        end
    end

    // synthesis translate_off
    initial if (LAT != 1) $error("mm_bram_port_p1: LAT must be 1, got %0d", LAT);
    // synthesis translate_on
endmodule
