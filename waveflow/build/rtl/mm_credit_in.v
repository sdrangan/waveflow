// mm_credit_in.v -- request-bus leaf: a memory-mapped CREDIT window (plans/mm_credit_stream.md D2).
//
// The producer end of a CreditStreamIF routed over the bus.  The consumer's bus writer writes its
// CUMULATIVE count of words consumed; the producer kernel receives it on an AXI-Stream master.
//
// WRITES (any address) replace the value -- a latest-value register, never a queue.  A write is
// accepted every cycle (req_ready is always high for a write), so the bus never waits on this view:
// the newest cumulative count is the whole truth (rule 1 of reverse_stream.py), so overwriting a value
// the kernel has not taken yet loses nothing, and the register cannot saturate (rule 4's hazard).
//
// The kernel side: TVALID while a value has been written and not yet taken; TDATA is the NEWEST value.
// A write in the cycle the kernel takes the old value leaves the new one pending.  TLAST is held high:
// every value is a one-word message.
//
// READS (any address) return the current value: debugging only, no side effects.
`timescale 1ns/1ps
module mm_credit_in #(
    parameter integer DW  = 64,
    parameter integer LAW = 12
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
    output wire              rsp_err,
    // AXI-Stream master (to the kernel's credit port)
    output wire [DW-1:0]     m_axis_TDATA,
    output wire              m_axis_TVALID,
    input  wire              m_axis_TREADY,
    output wire              m_axis_TLAST
);
    reg  [DW-1:0] value_q;
    reg           pend_q;             // a value written and not yet taken by the kernel

    wire rd_ready = !rsp_valid || rsp_ready;
    assign req_ready = req_we ? 1'b1 : rd_ready;
    wire wr_hs = req_valid && req_we;
    wire rd_hs = req_valid && !req_we && rd_ready;

    assign m_axis_TDATA  = value_q;
    assign m_axis_TVALID = pend_q;
    assign m_axis_TLAST  = 1'b1;
    assign rsp_err       = 1'b0;

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            value_q <= 0; pend_q <= 1'b0; rsp_valid <= 1'b0; rsp_rdata <= 0;
        end else begin
            if (wr_hs) begin
                value_q <= req_wdata;
                pend_q  <= 1'b1;          // a write wins over a take in the same cycle
            end else if (m_axis_TVALID && m_axis_TREADY) begin
                pend_q  <= 1'b0;
            end
            if (rd_hs) begin
                rsp_valid <= 1'b1;
                rsp_rdata <= value_q;
            end else if (rsp_ready) begin
                rsp_valid <= 1'b0;
            end
        end
    end
endmodule
