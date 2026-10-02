// axi_slave_front.v -- AXI4 (full) slave -> the adaptor's in-order request bus.
//
// plans/mm_slave_adaptor.md, "Realization".  Hand-written and fixed: widths ride on parameters, the
// file is never rewritten.  Every memory-mapped view (queue, register bank, BRAM window) is a leaf on
// the request bus below; this module is the only one that speaks AXI.
//
// ORDERING (the plan's guarantee 1).  One AXI transaction is in service at a time, reads and writes
// alike, so the leaves see requests in the order the transactions were accepted.  A write to a BRAM
// window therefore reaches the memory before a later doorbell write is even decoded.  When an AW and
// an AR are both waiting, the front alternates between them, so neither direction can starve.
//
// REQUEST BUS (one beat per request, valid/ready on both channels):
//   req_valid/req_ready, req_we, req_addr[LAW-1:0] (local BYTE address of the beat), req_wdata
//   rsp_valid/rsp_ready, rsp_rdata, rsp_err         (reads only; a write is done at req handshake)
// A leaf must hold rsp_* until rsp_ready, and may accept the next read request in the same cycle its
// response is taken (req_ready = !rsp_valid || rsp_ready).  The front keeps at most one read request
// outstanding and issues the next one in the cycle the previous response is taken, so a leaf with a
// one-cycle registered response sustains one beat per cycle.
//
// ERRORS -> SLVERR, and the beat never reaches a leaf:
//   * AWSIZE/ARSIZE narrower or wider than the bus (a stream cannot carry a partial word);
//   * WRAP bursts;
//   * a W beat whose WSTRB is not all ones (consumed and dropped, so the burst still completes).
// A leaf's rsp_err also returns SLVERR on that read beat.  FIXED bursts repeat one address; INCR
// bursts advance by one word per beat.
`timescale 1ns/1ps
module axi_slave_front #(
    parameter integer DW  = 64,   // data width (bits); also the request bus width
    parameter integer AW  = 32,   // AXI address width
    parameter integer IDW = 1,    // AXI ID width
    parameter integer LAW = 12    // local address bits passed to the leaves (window = 2**LAW bytes)
) (
    input  wire                 ap_clk,
    input  wire                 ap_rst_n,
    // AXI4 slave
    input  wire [IDW-1:0]       s_axi_AWID,
    input  wire [AW-1:0]        s_axi_AWADDR,
    input  wire [7:0]           s_axi_AWLEN,
    input  wire [2:0]           s_axi_AWSIZE,
    input  wire [1:0]           s_axi_AWBURST,
    input  wire                 s_axi_AWVALID,
    output wire                 s_axi_AWREADY,
    input  wire [DW-1:0]        s_axi_WDATA,
    input  wire [DW/8-1:0]      s_axi_WSTRB,
    input  wire                 s_axi_WLAST,
    input  wire                 s_axi_WVALID,
    output wire                 s_axi_WREADY,
    output wire [IDW-1:0]       s_axi_BID,
    output wire [1:0]           s_axi_BRESP,
    output wire                 s_axi_BVALID,
    input  wire                 s_axi_BREADY,
    input  wire [IDW-1:0]       s_axi_ARID,
    input  wire [AW-1:0]        s_axi_ARADDR,
    input  wire [7:0]           s_axi_ARLEN,
    input  wire [2:0]           s_axi_ARSIZE,
    input  wire [1:0]           s_axi_ARBURST,
    input  wire                 s_axi_ARVALID,
    output wire                 s_axi_ARREADY,
    output wire [IDW-1:0]       s_axi_RID,
    output wire [DW-1:0]        s_axi_RDATA,
    output wire [1:0]           s_axi_RRESP,
    output wire                 s_axi_RLAST,
    output wire                 s_axi_RVALID,
    input  wire                 s_axi_RREADY,
    // request bus (to the leaves)
    output wire                 req_valid,
    input  wire                 req_ready,
    output wire                 req_we,
    output wire [LAW-1:0]       req_addr,
    output wire [DW-1:0]        req_wdata,
    input  wire                 rsp_valid,
    output wire                 rsp_ready,
    input  wire [DW-1:0]        rsp_rdata,
    input  wire                 rsp_err
);
    localparam integer NB = DW / 8;
    // log2(NB): the one AxSIZE this front accepts.
    localparam [2:0] SIZE = (NB == 1) ? 3'd0 : (NB == 2) ? 3'd1 : (NB == 4) ? 3'd2 :
                            (NB == 8) ? 3'd3 : (NB == 16) ? 3'd4 : (NB == 32) ? 3'd5 :
                            (NB == 64) ? 3'd6 : 3'd7;
    localparam [1:0] S_IDLE = 2'd0, S_WR = 2'd1, S_B = 2'd2, S_RD = 2'd3;
    localparam [1:0] OKAY = 2'b00, SLVERR = 2'b10;

    reg [1:0]     state;
    reg           prefer_rd;        // alternation: who wins when AW and AR are both waiting
    reg [IDW-1:0] id_q;
    reg [LAW-1:0] addr_q;           // local address of the current beat
    reg           fixed_q;          // FIXED burst: the address does not advance
    reg           bad_q;            // the whole transaction is refused (size / burst type)
    reg           err_q;            // some beat failed: the response will be SLVERR
    reg [8:0]     left_q;           // read beats still to request
    reg           outst_q;          // a read request is outstanding at the leaf
    reg           rerr_q;           // refused read: beats are synthesized here, not requested

    wire pick_wr = s_axi_AWVALID && (!s_axi_ARVALID || !prefer_rd);
    wire pick_rd = s_axi_ARVALID && (!s_axi_AWVALID ||  prefer_rd);

    assign s_axi_AWREADY = (state == S_IDLE) && pick_wr;
    assign s_axi_ARREADY = (state == S_IDLE) && pick_rd;

    // --- write data: a good beat goes to the leaf; a bad one is swallowed here ---
    wire beat_ok   = !bad_q && (&s_axi_WSTRB);
    assign s_axi_WREADY = (state == S_WR) && (beat_ok ? req_ready : 1'b1);
    wire w_hs      = s_axi_WVALID && s_axi_WREADY;

    // --- read: request the next beat when nothing is outstanding, or as the outstanding one is taken
    wire r_take    = (state == S_RD) && s_axi_RVALID && s_axi_RREADY;
    wire rd_issue  = (state == S_RD) && !rerr_q && (left_q != 0) && (!outst_q || r_take);

    assign req_valid = ((state == S_WR) && s_axi_WVALID && beat_ok) || rd_issue;
    assign req_we    = (state == S_WR);
    assign req_addr  = addr_q;
    assign req_wdata = s_axi_WDATA;
    wire   req_hs    = req_valid && req_ready;

    // A refused read still owes the master its beats: present them here, SLVERR, never asking a leaf
    // (a queue-out leaf would pop on every one of them).
    assign s_axi_RVALID = (state == S_RD) && (rerr_q ? (left_q != 0) : (outst_q && rsp_valid));
    assign s_axi_RDATA  = rerr_q ? {DW{1'b0}} : rsp_rdata;
    assign s_axi_RRESP  = (rerr_q || rsp_err) ? SLVERR : OKAY;
    assign s_axi_RLAST  = rerr_q ? (left_q == 1) : (outst_q && left_q == 0);
    assign s_axi_RID    = id_q;
    assign rsp_ready    = (state == S_RD) && !rerr_q && outst_q && s_axi_RREADY;

    assign s_axi_BVALID = (state == S_B);
    assign s_axi_BRESP  = err_q ? SLVERR : OKAY;
    assign s_axi_BID    = id_q;

    wire [LAW-1:0] next_addr = fixed_q ? addr_q : addr_q + NB[LAW-1:0];

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            state <= S_IDLE; prefer_rd <= 1'b0; id_q <= 0; addr_q <= 0; fixed_q <= 1'b0;
            bad_q <= 1'b0; err_q <= 1'b0; left_q <= 0; outst_q <= 1'b0; rerr_q <= 1'b0;
        end else begin
            case (state)
            S_IDLE: begin
                if (s_axi_AWVALID && s_axi_AWREADY) begin
                    state   <= S_WR;
                    id_q    <= s_axi_AWID;
                    addr_q  <= s_axi_AWADDR[LAW-1:0];
                    fixed_q <= (s_axi_AWBURST == 2'b00);
                    bad_q   <= (s_axi_AWSIZE != SIZE) || (s_axi_AWBURST == 2'b10) || (s_axi_AWBURST == 2'b11);
                    err_q   <= (s_axi_AWSIZE != SIZE) || (s_axi_AWBURST == 2'b10) || (s_axi_AWBURST == 2'b11);
                    prefer_rd <= 1'b1;
                end else if (s_axi_ARVALID && s_axi_ARREADY) begin
                    state   <= S_RD;
                    id_q    <= s_axi_ARID;
                    addr_q  <= s_axi_ARADDR[LAW-1:0];
                    fixed_q <= (s_axi_ARBURST == 2'b00);
                    rerr_q  <= (s_axi_ARSIZE != SIZE) || (s_axi_ARBURST == 2'b10) || (s_axi_ARBURST == 2'b11);
                    left_q  <= {1'b0, s_axi_ARLEN} + 9'd1;
                    outst_q <= 1'b0;
                    prefer_rd <= 1'b0;
                end
            end
            S_WR: begin
                if (w_hs) begin
                    if (!beat_ok) err_q <= 1'b1;
                    addr_q <= next_addr;
                    if (s_axi_WLAST) state <= S_B;
                end
            end
            S_B: begin
                if (s_axi_BREADY) begin state <= S_IDLE; err_q <= 1'b0; bad_q <= 1'b0; end
            end
            S_RD: begin
                if (rerr_q) begin
                    if (s_axi_RVALID && s_axi_RREADY) begin
                        left_q <= left_q - 9'd1;
                        if (left_q == 1) begin state <= S_IDLE; rerr_q <= 1'b0; end
                    end
                end else begin
                    if (req_hs) begin
                        addr_q <= next_addr;
                        left_q <= left_q - 9'd1;
                    end
                    // outstanding: set by an issue, cleared by a take that is not replaced
                    if (req_hs) outst_q <= 1'b1;
                    else if (r_take) outst_q <= 1'b0;
                    if (r_take && left_q == 0 && !req_hs) state <= S_IDLE;
                end
            end
            endcase
        end
    end
endmodule
