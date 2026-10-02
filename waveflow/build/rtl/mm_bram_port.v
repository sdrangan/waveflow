// mm_bram_port.v -- request-bus leaf: a memory-mapped window onto ONE port of a block RAM.
//
// plans/mm_slave_adaptor.md, "BRAM windows".  The memory itself is bram_t2p.v, beside the kernel as
// always: this leaf drives its port A, the kernel keeps port B.  Data does not travel as stream
// messages here -- that would waste the memory's random access -- but synchronization still does: a
// doorbell (a queue or register write in the SAME adaptor) after the data is ordered behind it by the
// front, which serves one AXI transaction at a time.  That is the plan's ordering guarantee 1, and
// this leaf's part in it is that a write is DONE at its request handshake: the word is in the memory
// at the next clock edge, before the front can even decode the next transaction.
//
// Window: word i at local byte i * (DW/8).  Words at or beyond 2**BAW are outside the memory: a write
// there is dropped, a read answers 0 with SLVERR.
//
// Reads: one in flight.  The address goes to the memory on the request handshake and the data is
// taken LAT cycles later -- LAT MUST equal the memory's published READ_LATENCY (the generator reads
// it from bram_t2p.v, so the two have one source).  A read costs LAT + 1 cycles at the leaf.
`timescale 1ns/1ps
module mm_bram_port #(
    parameter integer DW  = 64,
    parameter integer LAW = 12,
    parameter integer BAW = 9,      // memory address bits (words)
    parameter integer LAT = 1       // the memory's read latency
) (
    input  wire              ap_clk,
    input  wire              ap_rst_n,
    // request bus (from axi_slave_front, or a decoder)
    input  wire              req_valid,
    output wire              req_ready,
    input  wire              req_we,
    input  wire [LAW-1:0]    req_addr,
    input  wire [DW-1:0]     req_wdata,
    output reg               rsp_valid,
    input  wire              rsp_ready,
    output reg  [DW-1:0]     rsp_rdata,
    output reg               rsp_err,
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

    reg  [7:0] wait_q;              // cycles until the in-flight read's data is on bram_dout
    reg        busy_q;              // a read is in flight
    reg        oob_q;               // ...and it was outside the memory

    wire rd_ready = !busy_q && (!rsp_valid || rsp_ready);
    assign req_ready = req_we ? 1'b1 : rd_ready;
    wire wr_hs = req_valid && req_we;
    wire rd_hs = req_valid && !req_we && rd_ready;

    assign bram_en   = (wr_hs || rd_hs) && in_mem;
    assign bram_we   = (wr_hs && in_mem) ? 2'b11 : 2'b00;
    assign bram_addr = {{(32 - (LAW - WB)){1'b0}}, widx};
    assign bram_din  = req_wdata;

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            busy_q <= 1'b0; wait_q <= 0; oob_q <= 1'b0;
            rsp_valid <= 1'b0; rsp_rdata <= 0; rsp_err <= 1'b0;
        end else begin
            if (rsp_valid && rsp_ready) rsp_valid <= 1'b0;
            if (rd_hs) begin
                busy_q <= 1'b1; wait_q <= LAT; oob_q <= !in_mem;
            end else if (busy_q) begin
                if (wait_q <= 1) begin
                    busy_q    <= 1'b0;
                    rsp_valid <= 1'b1;
                    rsp_rdata <= oob_q ? {DW{1'b0}} : bram_dout;
                    rsp_err   <= oob_q;
                end else begin
                    wait_q <= wait_q - 1;
                end
            end
        end
    end
endmodule
