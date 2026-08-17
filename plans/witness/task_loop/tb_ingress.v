// tb_ingress.v — how continuously does a stream-read -> BRAM-write task consume its input?
//
// The converter-facing port of this shape is the INPUT, so the measurement is on the input
// handshake: with TVALID held high forever, every cycle the kernel does not take a word is a cycle
// an ADC would have lost one.
//
// The kernel module is chosen at compile time: `xvlog -d KERNEL=ing_n8 ...`.  One testbench for all
// four loop shapes, because a per-variant testbench is a place for the measurement to differ for a
// reason other than the design.
//
// WHAT IS PRINTED, and why each line is needed:
//
//   BEATS/CYCLES   sustained throughput.  1.000 means the task never stops reading.
//   GAP histogram  the deltas between consecutive beats.  A delta of 1 is a cycle inside the loop;
//                  anything larger is the loop boundary, and its SIZE is question 3's answer.
//   RAMP           a correctness check on the BRAM writes, so a "fast" result that is dropping or
//                  reordering words cannot pass as a good one.
`timescale 1ns/1ps
module tb;
    localparam DEPTH   = 4096;
    localparam NBEATS  = 400;    // enough to cross several boundaries at N=64
    localparam TIMEOUT = 20000;

    reg clk = 0, rst_n = 0;
    always #5 clk = ~clk;

    reg  [15:0] in_d = 0;
    reg         in_v = 0;
    wire        in_rdy;

    // BRAM pins.  This shape WRITES the memory, so the testbench only has to observe: a BRAM port
    // has no handshake and cannot refuse, which is exactly why this body can never be stalled by
    // anything except its input.
    wire [31:0] b_addr_a, b_addr_b;
    wire        b_en_a,   b_en_b;
    wire [15:0] b_din_a,  b_din_b;
    wire [1:0]  b_we_a,   b_we_b;

    reg [15:0] mem [0:DEPTH-1];

    // Vitis suffixes a by-reference argument's ports with `_r`: `buf` -> `buf_r_*`, `in` -> `in_r_*`.
    // Verified identical across all four loop shapes, which is what lets one testbench serve them.
    `KERNEL dut (
        .ap_clk(clk), .ap_rst_n(rst_n),
        .in_r_TDATA(in_d), .in_r_TVALID(in_v), .in_r_TREADY(in_rdy),
        .buf_r_Addr_A(b_addr_a), .buf_r_EN_A(b_en_a), .buf_r_Din_A(b_din_a),
        .buf_r_Dout_A(16'd0), .buf_r_WEN_A(b_we_a), .buf_r_Clk_A(), .buf_r_Rst_A(),
        .buf_r_Addr_B(b_addr_b), .buf_r_EN_B(b_en_b), .buf_r_Din_B(b_din_b),
        .buf_r_Dout_B(16'd0), .buf_r_WEN_B(b_we_b), .buf_r_Clk_B(), .buf_r_Rst_B()
    );

    // The memory, behaviourally.  Vitis addresses a BRAM port in BYTES for a 16-bit word, so the
    // word index is the address shifted by one -- getting that wrong would shift the ramp and the
    // check below would catch it.
    integer nwrites = 0;
    always @(posedge clk) begin
        if (b_en_a && |b_we_a) begin
            mem[b_addr_a >> 1] <= b_din_a;
            nwrites = nwrites + 1;
        end
    end

    // ---- the measurement -------------------------------------------------------------------
    integer cyc = 0, nbeat = 0, i, d;
    integer beat_cyc [0:NBEATS-1];
    reg     measuring = 0;

    always @(posedge clk) begin
        if (measuring) cyc = cyc + 1;
        if (measuring && in_v && in_rdy && nbeat < NBEATS) begin
            beat_cyc[nbeat] = cyc;
            nbeat = nbeat + 1;
        end
    end

    integer gap1 = 0, gapn = 0, gmin = 1000000, gmax = 0, gsum = 0;
    integer span, errors = 0;

    initial begin
        repeat (10) @(posedge clk); rst_n = 1; repeat (5) @(posedge clk);
        // TVALID high forever: the converter never waits, which is the whole premise.
        @(negedge clk); in_v = 1; in_d = 16'd100;
        measuring = 1;
        // A ramp, so a dropped or reordered word is visible in the memory rather than only in a count.
        while (nbeat < NBEATS && cyc < TIMEOUT) begin
            @(posedge clk);
            if (in_v && in_rdy) @(negedge clk) in_d = in_d + 1;
        end
        @(negedge clk); in_v = 0;
        repeat (20) @(posedge clk);

        span = beat_cyc[nbeat-1] - beat_cyc[0];
        for (i = 1; i < nbeat; i = i + 1) begin
            d = beat_cyc[i] - beat_cyc[i-1];
            if (d == 1) gap1 = gap1 + 1;
            else begin
                gapn = gapn + 1; gsum = gsum + d;
                if (d < gmin) gmin = d;
                if (d > gmax) gmax = d;
            end
        end

        for (i = 0; i < 64; i = i + 1)
            if (mem[i] !== 16'd100 + i) begin
                errors = errors + 1;
                if (errors < 5) $display("  RAMP MISMATCH mem[%0d] = %0d, expected %0d",
                                         i, mem[i], 100 + i);
            end

        $display("KERNEL=%s", `"`KERNEL`");
        $display("BEATS=%0d SPAN_CYCLES=%0d", nbeat, span);
        $display("THROUGHPUT_WORDS_PER_CYCLE=%0d.%03d",
                 nbeat / (span + 1), ((nbeat * 1000) / (span + 1)) % 1000);
        $display("GAPS_OF_1=%0d BOUNDARY_GAPS=%0d", gap1, gapn);
        if (gapn > 0)
            $display("BOUNDARY_GAP_MIN=%0d BOUNDARY_GAP_MAX=%0d BOUNDARY_GAP_MEAN_X100=%0d",
                     gmin, gmax, (gsum * 100) / gapn);
        else
            $display("BOUNDARY_GAP_MIN=0 BOUNDARY_GAP_MAX=0 BOUNDARY_GAP_MEAN_X100=0");
        $display("BRAM_WRITES=%0d RAMP_ERRORS=%0d", nwrites, errors);
        $display("TB-INGRESS: %s", (errors == 0 && nbeat == NBEATS) ? "PASS" : "FAIL");
        $finish;
    end
endmodule
