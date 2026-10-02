// mm_queue_out.v -- request-bus leaf: an AXI-Stream drained by memory-mapped reads.
//
// plans/mm_slave_adaptor.md, "Queue windows".  The kernel side is an ordinary AXIS slave.
//
// The window is split in two halves by the top local address bit:
//   * DATA half   (req_addr[LAW-1] == 0): a read POPS one word.  Reading an EMPTY queue returns 0 with
//                  SLVERR and pops nothing -- it never stalls RVALID, because a read that waits for
//                  data would hold the whole bus.  The master checks the occupancy first.
//   * STATUS half (req_addr[LAW-1] == 1): a read returns the OCCUPANCY (words available, 0..DEPTH),
//                  no side effects.
// A 4 KB window gives a 2 KB data half: 256 words at 64 bits, exactly one maximal AXI4 burst.
//
// Writes are accepted and DROPPED.  (The request bus carries no write error back; a stray write is
// harmless here and the plan's open question on write errors stays open.)
//
// TLAST is accepted and ignored: the data half delivers raw words.  Packet boundaries on this side
// are a Stage 1 open question in the plan.
`timescale 1ns/1ps
module mm_queue_out #(
    parameter integer DW    = 64,
    parameter integer LAW   = 12,
    parameter integer DEPTH = 512
) (
    input  wire              ap_clk,
    input  wire              ap_rst_n,
    // request bus (from axi_slave_front)
    input  wire              req_valid,
    output wire              req_ready,
    input  wire              req_we,
    input  wire [LAW-1:0]    req_addr,
    input  wire [DW-1:0]     req_wdata,
    output reg               rsp_valid,
    input  wire              rsp_ready,
    output reg  [DW-1:0]     rsp_rdata,
    output reg               rsp_err,
    // AXI-Stream slave (from the kernel)
    input  wire [DW-1:0]     s_axis_TDATA,
    input  wire              s_axis_TVALID,
    output wire              s_axis_TREADY,
    input  wire              s_axis_TLAST
);
    localparam integer CW = $clog2(DEPTH);

    wire [DW-1:0] fifo_dout;
    wire          fifo_full, fifo_empty;
    wire [CW:0]   fifo_count;

    wire rd_ready = !rsp_valid || rsp_ready;
    assign req_ready = req_we ? 1'b1 : rd_ready;
    wire rd_hs  = req_valid && !req_we && rd_ready;
    wire status = req_addr[LAW-1];
    wire pop    = rd_hs && !status && !fifo_empty;

    assign s_axis_TREADY = !fifo_full;
    mm_sync_fifo #(.W(DW), .DEPTH(DEPTH)) u_fifo (
        .clk(ap_clk), .rst_n(ap_rst_n),
        .push(s_axis_TVALID && !fifo_full), .din(s_axis_TDATA),
        .pop(pop),
        .dout(fifo_dout), .full(fifo_full), .empty(fifo_empty), .count(fifo_count)
    );

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            rsp_valid <= 1'b0; rsp_rdata <= 0; rsp_err <= 1'b0;
        end else begin
            if (rd_hs) begin
                rsp_valid <= 1'b1;
                rsp_rdata <= status ? fifo_count : (fifo_empty ? {DW{1'b0}} : fifo_dout);
                rsp_err   <= !status && fifo_empty;
            end else if (rsp_ready) begin
                rsp_valid <= 1'b0;
            end
        end
    end
endmodule
