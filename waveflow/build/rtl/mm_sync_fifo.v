// mm_sync_fifo.v -- single-clock first-word-fall-through FIFO for the adaptor's queue leaves.
//
// plans/mm_slave_adaptor.md.  `dout` is valid whenever `!empty` (no read latency), which is what lets
// a queue leaf answer a read request with a one-cycle registered response.  DEPTH must be a power of
// two; `count` is exact (0..DEPTH) so a leaf can publish occupancy or vacancy without arithmetic on
// the pointers.
`timescale 1ns/1ps
module mm_sync_fifo #(
    parameter integer W     = 65,
    parameter integer DEPTH = 512
) (
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire                     push,
    input  wire [W-1:0]             din,
    input  wire                     pop,
    output wire [W-1:0]             dout,
    output wire                     full,
    output wire                     empty,
    output reg  [$clog2(DEPTH):0]   count
);
    localparam integer PW = $clog2(DEPTH);
    reg [W-1:0]  mem [0:DEPTH-1];
    reg [PW-1:0] wr_ptr, rd_ptr;

    assign full  = (count == DEPTH);
    assign empty = (count == 0);
    assign dout  = mem[rd_ptr];

    wire do_push = push && !full;
    wire do_pop  = pop && !empty;

    always @(posedge clk) begin
        if (do_push) mem[wr_ptr] <= din;
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            wr_ptr <= 0; rd_ptr <= 0; count <= 0;
        end else begin
            if (do_push) wr_ptr <= wr_ptr + 1'b1;
            if (do_pop)  rd_ptr <= rd_ptr + 1'b1;
            count <= count + (do_push ? 1'b1 : 1'b0) - (do_pop ? 1'b1 : 1'b0);
        end
    end
endmodule
