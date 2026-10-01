# run.tcl -- Vitis HLS for the streaming polynomial accelerator.
#
#   WAVEFLOW_POLY_STAGE=csim   vitis-run --mode hls --tcl run.tcl   every scenario, C simulation
#   WAVEFLOW_POLY_STAGE=synth  vitis-run --mode hls --tcl run.tcl   C synthesis + RTL
#                                                                   co-simulation of the
#                                                                   timing scenario
#
# (The stage is an environment variable because vitis-run 2025.1 has no --tclargs.)
#
# The kernel boundary (gen/poly.cpp, gen/poly.hpp) is generated; the kernel body
# (poly_body_impl.tpp, included from gen/poly.hpp) and the testbench (poly_tb.cpp) are
# hand-written.  The testbench writes each scenario's response to data/<scenario>/<stage>/.
#
# Environment: WAVEFLOW_POLY_CLK_PERIOD_NS (default 10), WAVEFLOW_POLY_TRACE_LEVEL
# (none | port | all, default none; port records the VCD the timing figures are drawn from).

set script_dir [file dirname [file normalize [info script]]]
set stage ""
if {[info exists ::env(WAVEFLOW_POLY_STAGE)]} {
    set stage $::env(WAVEFLOW_POLY_STAGE)
}
if {$stage ni {csim synth}} {
    puts "WAVEFLOW_ERROR: set WAVEFLOW_POLY_STAGE to csim or synth (got '$stage')."
    exit 1
}

open_project -reset waveflow_poly_proj
set_top poly
add_files gen/poly.cpp -cflags "-I."
add_files -tb poly_tb.cpp -cflags "-I."
set streamutils_cpp [file join $script_dir "include" "streamutils.cpp"]
if {[file exists $streamutils_cpp]} {
    add_files -tb $streamutils_cpp
}

open_solution -reset "solution1"
set_part {xc7z020clg484-1}
set clk_period_ns 10
if {[info exists ::env(WAVEFLOW_POLY_CLK_PERIOD_NS)]} {
    set clk_period_ns $::env(WAVEFLOW_POLY_CLK_PERIOD_NS)
}
create_clock -period $clk_period_ns
set trace_level "none"
if {[info exists ::env(WAVEFLOW_POLY_TRACE_LEVEL)]} {
    set trace_level $::env(WAVEFLOW_POLY_TRACE_LEVEL)
}
set data_dir [file join $script_dir "data"]

if {$stage eq "csim"} {
    if {[catch {csim_design -argv "$data_dir csim"} res]} {
        puts "WAVEFLOW_ERROR: poly C simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: poly C simulation passed."
} else {
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: poly C synthesis failed."
        puts $res
        exit 1
    }
    if {[catch {cosim_design -argv "$data_dir cosim timing" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: poly RTL co-simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: poly C synthesis and RTL co-simulation passed."
}
exit 0
