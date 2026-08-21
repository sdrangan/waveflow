// tb_player.v — does a bounded pipelined loop EMIT CONTINUOUSLY, or store-and-forward?
//
// This is question 2, and it is the one with no local evidence.  The concern is that a firing
// buffers all N outputs and releases them at the end; standard HLS stream semantics say a `write()`
// is scheduled at its cycle, but the two bodies in this repo that could have shown it do not — the
// relay buffers by construction and the ingress is one word per firing.
//
// TWO PHASES, because throughput alone cannot tell the two apart.
//
//   PHASE 1 — free run, TREADY high forever.
//       Continuous emission and store-and-forward BOTH produce runs of back-to-back beats; what
//       separates them is the PERIOD.  Continuous sustains ~1 word/cycle.  Store-and-forward spends
//       N cycles computing and N draining, so it sustains ~0.5.
//
//   PHASE 2 — back-pressure, TREADY held LOW mid-firing.
//       The crisp one, and it does not depend on interpreting a ratio.  Count BRAM reads while the
//       output is stalled.  A body that buffers keeps reading into its buffer, so reads run on past
//       the stalled writes; a body whose write is IN the pipeline stalls the whole pipeline within
//       its depth and stops reading.  The number of extra reads IS the size of the hidden buffer.
//
// The memory returns `addr + 100` at latency 1 — a ramp, so a read at the wrong address or the
// wrong latency shows up as a value error rather than passing silently.
`timescale 1ns/1ps
module tb;
    localparam NBEATS  = 400;
    localparam TIMEOUT = 20000;
    localparam STALL   = 40;     // longer than any plausible pipeline depth, shorter than N=64

    reg clk = 0, rst_n = 0;
    always #5 clk = ~clk;

    wire [15:0] out_d;
    wire        out_v;
    reg         rdy = 0;        // TREADY.  Deliberately NOT named out_r: that is the DUT's port prefix.

    wire [31:0] b_addr_a, b_addr_b;
    wire        b_en_a,   b_en_b;
    wire [15:0] b_din_a,  b_din_b;
    wire [1:0]  b_we_a,   b_we_b;
    reg  [15:0] b_dout_a = 0;

    // Vitis suffixes a by-reference argument's ports with `_r`: `buf` -> `buf_r_*`, `out` -> `out_r_*`.
    `KERNEL dut (
        .ap_clk(clk), .ap_rst_n(rst_n),
        .out_r_TDATA(out_d), .out_r_TVALID(out_v), .out_r_TREADY(rdy),
        .buf_r_Addr_A(b_addr_a), .buf_r_EN_A(b_en_a), .buf_r_Din_A(b_din_a),
        .buf_r_Dout_A(b_dout_a), .buf_r_WEN_A(b_we_a), .buf_r_Clk_A(), .buf_r_Rst_A(),
        .buf_r_Addr_B(b_addr_b), .buf_r_EN_B(b_en_b), .buf_r_Din_B(b_din_b),
        .buf_r_Dout_B(16'd0), .buf_r_WEN_B(b_we_b), .buf_r_Clk_B(), .buf_r_Rst_B()
    );

    // The memory: latency 1, value = word index + 100.  Vitis addresses in BYTES for a 16-bit word.
    integer nreads = 0;
    always @(posedge clk) begin
        if (b_en_a && !(|b_we_a)) begin
            b_dout_a <= (b_addr_a >> 1) + 16'd100;
            nreads = nreads + 1;
        end
    end

    // ---- the measurement -------------------------------------------------------------------
    integer cyc = 0, nbeat = 0, i, d;
    integer beat_cyc [0:NBEATS-1];
    reg     [15:0] beat_val [0:NBEATS-1];
    reg     measuring = 0;

    // `nbeat` is capped by the array; `total_beats` is NOT, and the resume check uses that one.
    // The first version of this testbench counted the resume with `nbeat`, which had already hit its
    // cap by then — so it printed 0 whatever the design did, and a wedged design would have passed.
    integer total_beats = 0;

    always @(posedge clk) begin
        if (measuring) cyc = cyc + 1;
        if (out_v && rdy) total_beats = total_beats + 1;
        if (measuring && out_v && rdy && nbeat < NBEATS) begin
            beat_cyc[nbeat] = cyc;
            beat_val[nbeat] = out_d;
            nbeat = nbeat + 1;
        end
    end

    integer gap1 = 0, gapn = 0, gmin = 1000000, gmax = 0, gsum = 0;
    integer span, errors = 0;
    integer reads_at_stall, reads_during_stall, beats_at_stall;

    // ---- PHASE 0 instrumentation: what does the task do while ap_rst_n is LOW? ------------------
    //
    // A player that asserts TVALID during reset puts a word on the DAC before the design is
    // initialised -- garbage at power-up, and a hardware bug someone would spend a day on.  This is
    // the gap the README named as the one to close first, and `while (1)` changes its shape: an
    // infinite loop has no firing boundary at which the runtime could hold it off.
    //
    // Measured with TREADY HIGH throughout reset, which is the hostile case and the realistic one --
    // RfdcDacSlave drives it from cycle 0, and a real converter's input FIFO is ready as soon as it
    // is powered.  Both counters below are needed: `valid` catches an emission the sink happened not
    // to take, `beats` catches one it did.
    integer rst_valid_cycles = 0, rst_beats = 0;
    always @(posedge clk) if (!rst_n) begin
        if (out_v)        rst_valid_cycles = rst_valid_cycles + 1;
        if (out_v && rdy) rst_beats        = rst_beats + 1;
    end

    initial begin
        // TREADY high from the very start, before reset is even released.
        @(negedge clk); rdy = 1;
        repeat (10) @(posedge clk); rst_n = 1;
        $display("RESET_TVALID_CYCLES=%0d RESET_BEATS=%0d", rst_valid_cycles, rst_beats);
        // How many words land in the first 8 cycles after release, before anything has filled the
        // buffer?  Nonzero is EXPECTED and is the documented free-running startup transient, not a
        // reset bug -- it is reported separately so the two are not confused with each other.
        beats_at_stall = total_beats;
        repeat (8) @(posedge clk);
        $display("POST_RESET_BEATS_8=%0d", total_beats - beats_at_stall);
        repeat (5) @(posedge clk);

        // ---- PHASE 1: free run ---------------------------------------------------------------
        @(negedge clk); rdy = 1;
        measuring = 1;
        while (nbeat < NBEATS && cyc < TIMEOUT) @(posedge clk);
        measuring = 0;

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
        // The values must be the ramp, in order, with no repeats or holes.
        for (i = 1; i < nbeat; i = i + 1)
            if (beat_val[i] !== ((beat_val[i-1] + 1) & 16'hFFFF)) begin
                errors = errors + 1;
                if (errors < 5) $display("  RAMP BREAK at beat %0d: %0d after %0d",
                                         i, beat_val[i], beat_val[i-1]);
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
        $display("RAMP_ERRORS=%0d", errors);

        // ---- PHASE 2: back-pressure ------------------------------------------------------------
        // Stall the output and watch the memory.  This is the store-and-forward discriminator.
        @(negedge clk); rdy = 0;
        reads_at_stall = nreads;
        repeat (STALL) @(posedge clk);
        reads_during_stall = nreads - reads_at_stall;
        $display("READS_DURING_STALL=%0d STALL_CYCLES=%0d", reads_during_stall, STALL);
        $display("VERDICT_STORE_AND_FORWARD=%0d", (reads_during_stall > 4) ? 1 : 0);

        // Release, and confirm it resumes rather than having wedged.  A design that deadlocked on
        // back-pressure would look identical to a healthy one in every number above.
        beats_at_stall = total_beats;
        @(negedge clk); rdy = 1;
        repeat (200) @(posedge clk);
        $display("RESUMED_BEATS=%0d", total_beats - beats_at_stall);

        // The threshold is LOOSE on purpose.  It only has to separate "wedged" (0 beats) from any
        // healthy rate, and the healthy rates here span 3x -- ply_1 emits one word per 3 cycles, so
        // 200 cycles buys it ~67 beats where ply_w gets 200.  A tighter bound would be re-asserting
        // the throughput that the phase-1 table already measures, and the first version of this line
        // did exactly that and failed ply_1 for running at its correct speed.
        $display("TB-PLAYER: %s",
                 (errors == 0 && nbeat > 0 && (total_beats - beats_at_stall) > 20)
                 ? "PASS" : "FAIL");
        $finish;
    end
endmodule
