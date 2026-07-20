// Test A (timing-trace feasibility): does $dumpvars fire under `xelab -dll` + XSI?
//
// Elaborated as a SECOND top alongside `mem_copy` (see run_trace.bat), so the XSI top -- and
// therefore every port number the BFM resolves -- is untouched.  A pass-through wrapper would
// also work but requires mirroring the whole AXI-MM + AXIS port list by hand; that tedium is
// exactly what would fail for reasons unrelated to what this is testing.
//
// Level 1 = this scope's OWN signals, no descent into children.  That is the point: a level-0
// dump of the full mem_copy tree is thousands of signals, and we only want the boundary here.
`timescale 1 ns / 1 ps

module vcd_dumper;
    initial begin
        $dumpfile("mem_copy_trace.vcd");
        $dumpvars(1, mem_copy);
    end
endmodule
