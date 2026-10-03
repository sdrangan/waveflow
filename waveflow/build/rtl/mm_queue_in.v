// mm_queue_in.v -- request-bus leaf: a memory-mapped window whose writes push an AXI-Stream.
//
// plans/mm_slave_adaptor.md, "Queue windows".  The kernel side is an ordinary AXIS master.
//
// WRITES to the LOWER half of the window (an INCR burst advances the address every beat, so a
// queue cannot be one address; an AXI4 burst is at most 256 beats, 2 KB at 64 bits, so a burst from
// the base stays in the lower half).  The stream is framed IN-BAND: each packet is `[len | data x len]`, the
// header's low 32 bits being `len`.  The header is consumed here; the `len` data words are pushed,
// the last with TLAST.  Because the leaf counts words, a packet may span any number of bursts, and an
// interconnect that splits a burst cannot move a packet boundary.  `len = 0` is an empty packet:
// nothing is pushed and the next word is a header again.
//
// A FULL FIFO HOLDS req_ready LOW, which the front turns into WREADY low: the master's burst
// stalls.  That is the modeled behaviour, not a defect, and a producer whose bus port carries other
// traffic must avoid it by reading the vacancy first (see below) or by holding credits.
//
// READS (any address) return the VACANCY: free data slots, 0..DEPTH, in the low bits.  No side
// effects -- reading it twice is harmless.  A header occupies no slot.
//
// INTERRUPT (plans/mm_irq.md D2).  A write to the UPPER half sets the threshold (and pushes nothing);
// `irq` is high while VACANCY >= threshold.  Threshold 0 -- the reset value -- holds it low, so a
// master that never writes one sees the leaf exactly as before.
`timescale 1ns/1ps
module mm_queue_in #(
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
    output wire              rsp_err,
    // AXI-Stream master (to the kernel)
    output wire [DW-1:0]     m_axis_TDATA,
    output wire              m_axis_TVALID,
    input  wire              m_axis_TREADY,
    output wire              m_axis_TLAST,
    // interrupt: high while the vacancy is at least the threshold
    output wire              irq
);
    localparam integer CW = $clog2(DEPTH);

    reg  [31:0] left_q;           // data words still owed by the current packet; 0 = expecting a header
    wire        in_hdr = (left_q == 0);

    wire [DW:0]  fifo_dout;
    wire         fifo_full, fifo_empty;
    wire [CW:0]  fifo_count;

    wire ctl = req_addr[LAW-1];       // the upper half: control (the interrupt threshold)
    reg  [CW:0] thresh_q;

    // A header and a control write are always accepted; a data word only when there is a slot.
    wire wr_ready = (ctl || in_hdr) ? 1'b1 : !fifo_full;
    wire rd_ready = !rsp_valid || rsp_ready;
    assign req_ready = req_we ? wr_ready : rd_ready;
    wire wr_hs = req_valid && req_we && wr_ready;
    wire rd_hs = req_valid && !req_we && rd_ready;

    wire push = wr_hs && !ctl && !in_hdr;
    mm_sync_fifo #(.W(DW + 1), .DEPTH(DEPTH)) u_fifo (
        .clk(ap_clk), .rst_n(ap_rst_n),
        .push(push), .din({left_q == 1, req_wdata}),
        .pop(m_axis_TVALID && m_axis_TREADY),
        .dout(fifo_dout), .full(fifo_full), .empty(fifo_empty), .count(fifo_count)
    );

    assign m_axis_TDATA  = fifo_dout[DW-1:0];
    assign m_axis_TLAST  = fifo_dout[DW];
    assign m_axis_TVALID = !fifo_empty;
    assign rsp_err       = 1'b0;
    assign irq           = (thresh_q != 0) && ((DEPTH - fifo_count) >= thresh_q);

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            left_q <= 0; rsp_valid <= 1'b0; rsp_rdata <= 0; thresh_q <= 0;
        end else begin
            if (wr_hs && ctl)  thresh_q <= req_wdata[CW:0];
            if (wr_hs && !ctl) left_q <= in_hdr ? req_wdata[31:0] : left_q - 1;
            if (rd_hs) begin
                rsp_valid <= 1'b1;
                rsp_rdata <= DEPTH - fifo_count;
            end else if (rsp_ready) begin
                rsp_valid <= 1'b0;
            end
        end
    end
endmodule
