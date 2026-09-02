// A true dual-port memory, hand-written.  Port A is the write side, port B the read side --
// which is what the kernel's two unidirectional BRAM interfaces expect.
//
// LATENCY 1: data appears on Dout the cycle AFTER the address is presented.  This must match the
// kernel's `#pragma HLS INTERFACE mode=bram ... latency=1`.  A registered-output BRAM would be 2,
// and the mismatch shows up as data shifted by one -- silently, which is why the ramp test exists.
`timescale 1ns/1ps
module bram_t2p #(parameter DW = 16, parameter AW = 10) (
    input               clk,
    // Port A -- write
    input      [31:0]   a_addr, input a_en, input [DW-1:0] a_din, input [1:0] a_we,
    output reg [DW-1:0] a_dout,
    // Port B -- read
    input      [31:0]   b_addr, input b_en, input [DW-1:0] b_din, input [1:0] b_we,
    output reg [DW-1:0] b_dout
);
    reg [DW-1:0] mem [0:(1<<AW)-1];

    // The published read latency, and the SINGLE SOURCE for it.  Waveflow reads this line and emits
    // the kernel's `#pragma HLS INTERFACE mode=bram ... latency=N` from it, so the two halves cannot
    // be authored independently and therefore cannot desynchronize
    // (waveflow/build/rtl_gen.py::rtl_read_latency).  `localparam`, not `parameter`: it is a
    // property of THIS implementation, not a knob -- changing it means changing the always block
    // below, and the value is what an instantiation must be told about, not what it may choose.
    localparam READ_LATENCY = 1;

    always @(posedge clk) begin
        if (a_en) begin
            if (|a_we) mem[a_addr[AW-1:0]] <= a_din;
            a_dout <= mem[a_addr[AW-1:0]];
        end
        if (b_en) begin
            if (|b_we) mem[b_addr[AW-1:0]] <= b_din;
            b_dout <= mem[b_addr[AW-1:0]];
        end
        // THE DESIGN INVARIANT, asserted where nothing else would check it: the reader must never
        // touch the address the writer is writing this cycle.  For a circular buffer that means
        // "rd trails wr"; if it ever fails, the data is whatever the BRAM's read-during-write mode
        // happens to be, and no tool will tell you.
        if (a_en && |a_we && b_en && (a_addr[AW-1:0] == b_addr[AW-1:0]))
            $error("bram_t2p: read-during-write collision at addr %0d", a_addr[AW-1:0]);
    end
endmodule
