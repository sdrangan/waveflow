// Test A, generalization check: does the top-scope-channel property hold for a 6-task
// load-compute-store pipeline, not just mem_copy's 3-task chain?
//
// Elaborated as a SECOND top alongside interleaver_canon (see run_trace.bat) so the XSI top and
// every BFM port number stay untouched.  Level 1 = this scope's OWN signals only, no descent.
`timescale 1 ns / 1 ps

module vcd_dumper;
    initial begin
        $dumpfile("interleaver_canon_trace.vcd");
        $dumpvars(1, interleaver_canon);
    end
endmodule
