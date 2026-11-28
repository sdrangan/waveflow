// mm_bram_port_pipe.v -- mm_bram_port.v's BRAM window, a read word every cycle, timing-closed.
//
// plans/bram_fullrate_port.md.  The same request-bus leaf onto port A of a block RAM as mm_bram_port.v
// -- its window, its write rule (the ordering guarantee), SLVERR outside the memory -- with its reads
// pipelined: one accepted a cycle, each answered LAT + 1 = 2 cycles after its request.  Selected by
// BramViewPipe (mm_adaptor_gen.py), a MemSlaveBramWindow with bram_leaf="mm_bram_port_pipe", or a
// FullRateMemoryMod.
//
// TWO REGISTERS on a read's way out, and why each is there:
//   * the BRAM's output register (bram_t2p, READ_LATENCY 1): the word, the cycle after the request;
//   * the response register -- entry 0 of the response FIFO below -- so the path from the memory to
//     the AXI read data starts at a flop: bram_dout ends at a flop here, and rsp_rdata leaves from one.
//     mm_bram_port_p1.v takes the BRAM's output straight to the read channel, which is why it is for a
//     memory that is simulated, not synthesized.
//
// A 4-beat INCR burst, the slot straight on an AXI port, behind axi_slave_front with RD_OUTST = 2
// (what this leaf needs: its answer comes 2 cycles after a request) and RREADY high:
//
//   cycle             0    1    2    3    4    5    6
//   AR handshake      AR
//   request (front)        r0   r1   r2   r3
//   bram_dout                   d0   d1   d2   d3
//   rsp (FIFO head)                  d0   d1   d2   d3
//   R beat                           R0   R1   R2   R3 RLAST
//
// An N-beat read: its RLAST beat N + 2 cycles after the AR handshake -- a beat a cycle, and 2 of
// latency.  (mm_bram_port.v: 2N + 1, a read in flight at a time and LAT + 1 cycles each.)
//
// THE RESPONSE FIFO.  The BRAM's output register holds a word for one cycle only (the next access
// rewrites it), so a read's word goes into the FIFO the cycle after its request, whatever rsp_ready
// does.  A read is therefore accepted only when the FIFO could take every read in flight: req_ready for
// a read is (words held + words on bram_dout) < DEPTH, from registers alone -- no path from rsp_ready.
// DEPTH = LAT + 2 = 3, sized from the diagram's steady state: each cycle one word is held (being
// answered) and one is on bram_dout, so the third place is what lets the next read be accepted without
// knowing whether this cycle's answer is taken.  With DEPTH 2 the leaf would accept a read every other
// cycle (or need rsp_ready in req_ready).  Behind a front with RD_OUTST = 2 it never holds more than
// two.  The FIFO is a shift register so that rsp_rdata / rsp_err are entry 0's flops: a take moves the
// rest down, and an arriving word lands at the first free entry after the take.
//
// WRITES: done at the request handshake, as in mm_bram_port.v -- the word is in the memory at the next
// edge, before the front can decode the next transaction (plans/mm_slave_adaptor.md, guarantee 1).
// req_ready is high for a write.  A write cannot disturb a read in flight: the read's word is taken
// from bram_dout in the cycle after its request, before an access in that cycle rewrites it (and the
// front finishes a read transaction before it serves another).
//
// Window: word i at local byte i * (DW/8).  Words at or beyond 2**BAW are outside the memory: a write
// there is dropped, a read answers 0 with rsp_err (SLVERR).  LAT must equal the memory's published
// READ_LATENCY (the generator reads it from bram_t2p.v) and only 1 is supported.
`timescale 1ns/1ps
module mm_bram_port_pipe #(
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
    localparam integer WB    = $clog2(DW / 8);
    localparam integer DEPTH = LAT + 2;             // the response FIFO (see the header)
    localparam integer CW    = $clog2(DEPTH + 1);   // its count, 0..DEPTH

    wire [LAW-1-WB:0] widx   = req_addr[LAW-1:WB];
    wire              in_mem = (widx < (1 << BAW));

    reg              fly_q;                 // a read's word is on bram_dout this cycle
    reg              fly_oob_q;             // ...and it was outside the memory
    reg  [CW-1:0]    cnt_q;                 // words held in the FIFO
    reg  [DW-1:0]    dat_q [0:DEPTH-1];     // the FIFO; entry 0 is the response register
    reg  [DEPTH-1:0] err_q;

    wire take    = (cnt_q != 0) && rsp_ready;
    wire rd_room = (cnt_q + fly_q) < DEPTH;
    assign req_ready = req_we ? 1'b1 : rd_room;
    wire wr_hs = req_valid && req_we;
    wire rd_hs = req_valid && !req_we && rd_room;

    assign bram_en   = (wr_hs || rd_hs) && in_mem;
    assign bram_we   = (wr_hs && in_mem) ? 2'b11 : 2'b00;
    assign bram_addr = {{(32 - (LAW - WB)){1'b0}}, widx};
    assign bram_din  = req_wdata;

    assign rsp_valid = (cnt_q != 0);
    assign rsp_rdata = dat_q[0];
    assign rsp_err   = err_q[0];

    // The arriving word's place: the first free entry once this cycle's take has moved the rest down.
    wire [CW-1:0] wpos     = take ? cnt_q - 1'b1 : cnt_q;
    wire [DW-1:0] fly_word = fly_oob_q ? {DW{1'b0}} : bram_dout;

    integer i;
    always @(posedge ap_clk) begin
        for (i = 0; i < DEPTH - 1; i = i + 1) begin
            if (fly_q && wpos == i) begin
                dat_q[i] <= fly_word;     err_q[i] <= fly_oob_q;
            end else if (take) begin
                dat_q[i] <= dat_q[i + 1]; err_q[i] <= err_q[i + 1];
            end
        end
        if (fly_q && wpos == DEPTH - 1) begin
            dat_q[DEPTH - 1] <= fly_word; err_q[DEPTH - 1] <= fly_oob_q;
        end
    end

    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin
            fly_q <= 1'b0; fly_oob_q <= 1'b0; cnt_q <= 0;
        end else begin
            fly_q     <= rd_hs;
            fly_oob_q <= rd_hs && !in_mem;
            cnt_q     <= cnt_q + (fly_q ? 1'b1 : 1'b0) - (take ? 1'b1 : 1'b0);
        end
    end

    // synthesis translate_off
    initial if (LAT != 1) $error("mm_bram_port_pipe: LAT must be 1, got %0d", LAT);
    // synthesis translate_on
endmodule
