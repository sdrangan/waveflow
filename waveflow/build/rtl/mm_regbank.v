// mm_regbank.v -- request-bus leaf: a shadow-and-commit config bank plus a latest-value status bank.
//
// plans/mm_slave_adaptor.md, "Register banks".  Window layout (local byte addresses, LAW >= 12):
//
//   [0,      W/2)    CONFIG SHADOW   word i at byte i*(DW/8).  Read/write.  Writing a field changes
//                                    nothing the kernel sees.
//   [W/2,    3W/4)   COMMIT          a write (any value) SNAPSHOTS the shadow and sends it to the
//                                    kernel as one packet of NCFG words on m_cfg (TLAST on the last).
//                                    A read returns the number of commits so far.
//   [3W/4,   W)      STATUS          word i = word i of the most recently COMPLETED status message.
//                                    Read-only, no side effects.
//
// Why shadow-and-commit: streaming each register write would let the kernel observe a half-updated
// configuration.  A commit is the one event at which the configuration changes -- the contract
// ap_start gives a host-activated kernel, made explicit for a free-running one.
//
// SNAPSHOT ISOLATION.  The packet is sent from a copy taken at the commit, so shadow writes after the
// commit never leak into a packet still being sent.  A second commit while a packet is still going
// out is held (req_ready low, so the master's WREADY stalls) until the first has gone: commits are
// never merged or dropped.
//
// STATUS.  The kernel pushes NSTAT-word messages on s_status (always ready).  A message completes on
// its NSTAT-th word or on TLAST, whichever is first; only then do its words become visible, all at
// once, so a read never mixes two messages.
`timescale 1ns/1ps
module mm_regbank #(
    parameter integer DW    = 64,
    parameter integer LAW   = 12,
    parameter integer NCFG  = 4,
    parameter integer NSTAT = 2
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
    // config packets (to the kernel)
    output wire [DW-1:0]     m_cfg_TDATA,
    output wire              m_cfg_TVALID,
    input  wire              m_cfg_TREADY,
    output wire              m_cfg_TLAST,
    // status messages (from the kernel)
    input  wire [DW-1:0]     s_status_TDATA,
    input  wire              s_status_TVALID,
    output wire              s_status_TREADY,
    input  wire              s_status_TLAST
);
    localparam integer WB = $clog2(DW / 8);          // byte -> word shift
    localparam integer IW = LAW - 2 - WB;            // word-index bits within a quarter window

    reg [DW-1:0] shadow   [0:NCFG-1];
    reg [DW-1:0] snap     [0:NCFG-1];
    reg [DW-1:0] stat_live[0:NSTAT-1];
    reg [DW-1:0] stat_pub [0:NSTAT-1];
    reg          sending;
    reg [15:0]   snd_i, st_i;
    reg [31:0]   ncommit;

    // Region decode on the top two local address bits: 0x/ = config, 10 = commit, 11 = status.
    wire [1:0]     region   = req_addr[LAW-1:LAW-2];
    wire           is_cfg   = !region[1];
    wire           is_commit = (region == 2'b10);
    wire           is_stat  = (region == 2'b11);
    wire [LAW-2-WB:0] cfg_i = req_addr[LAW-2:WB];    // config spans a half window
    wire [IW-1:0]  stat_i   = req_addr[LAW-3:WB];

    wire rd_ready = !rsp_valid || rsp_ready;
    // A commit while a packet is still going out waits; every other write is accepted at once.
    wire wr_ready = is_commit ? !sending : 1'b1;
    assign req_ready = req_we ? wr_ready : rd_ready;
    wire wr_hs = req_valid && req_we && wr_ready;
    wire rd_hs = req_valid && !req_we && rd_ready;
    assign rsp_err = 1'b0;

    assign m_cfg_TVALID = sending;
    assign m_cfg_TDATA  = snap[snd_i];
    assign m_cfg_TLAST  = (snd_i == NCFG - 1);
    wire cfg_beat = sending && m_cfg_TREADY;

    assign s_status_TREADY = 1'b1;
    wire st_beat = s_status_TVALID;
    wire st_done = st_beat && (s_status_TLAST || st_i == NSTAT - 1);

    integer k;
    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            sending <= 1'b0; snd_i <= 0; st_i <= 0; ncommit <= 0;
            rsp_valid <= 1'b0; rsp_rdata <= 0;
            for (k = 0; k < NCFG; k = k + 1) begin shadow[k] <= 0; snap[k] <= 0; end
            for (k = 0; k < NSTAT; k = k + 1) begin stat_live[k] <= 0; stat_pub[k] <= 0; end
        end else begin
            // --- bus writes ---
            if (wr_hs && is_cfg && cfg_i < NCFG) shadow[cfg_i] <= req_wdata;
            if (wr_hs && is_commit) begin
                for (k = 0; k < NCFG; k = k + 1) snap[k] <= shadow[k];
                sending <= 1'b1; snd_i <= 0; ncommit <= ncommit + 1;
            end
            // --- config packet out ---
            if (cfg_beat) begin
                if (snd_i == NCFG - 1) sending <= 1'b0;
                else snd_i <= snd_i + 1;
            end
            // --- status in: words land in the live copy; a completed message publishes at once ---
            if (st_beat) begin
                stat_live[st_i] <= s_status_TDATA;
                if (st_done) begin
                    for (k = 0; k < NSTAT; k = k + 1)
                        stat_pub[k] <= (k == st_i) ? s_status_TDATA : stat_live[k];
                    st_i <= 0;
                end else begin
                    st_i <= st_i + 1;
                end
            end
            // --- bus reads ---
            if (rd_hs) begin
                rsp_valid <= 1'b1;
                if (is_cfg)         rsp_rdata <= (cfg_i < NCFG) ? shadow[cfg_i] : {DW{1'b0}};
                else if (is_commit) rsp_rdata <= ncommit;
                else                rsp_rdata <= (stat_i < NSTAT) ? stat_pub[stat_i] : {DW{1'b0}};
            end else if (rsp_ready) begin
                rsp_valid <= 1'b0;
            end
        end
    end
endmodule
