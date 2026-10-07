# run.tcl -- Vitis HLS for the streaming polynomial accelerator.
#
#   WAVEFLOW_POLY_STAGE=csim   every scenario, C simulation
#   WAVEFLOW_POLY_STAGE=synth  C synthesis + RTL co-simulation of the timing scenario
#   WAVEFLOW_POLY_STAGE=vcd    C synthesis + RTL co-simulation of early_tlast_vcd, with port
#                              tracing, in its own project (the error-path waveform)
#
#   vitis-run --mode hls --tcl run.tcl
#
# (The stage is an environment variable because vitis-run 2025.1 has no --tclargs.)
#
# WAVEFLOW_POLY_WIDTH (32 | 64, default 32) picks the kernel top -- poly or poly_bw64, both
# in gen/poly.cpp -- the scenario data (data/w32 or data/w64) and the testbench's
# -DPOLY_WORD_BW.  Each width builds in its own project, one directory deep
# (w32_proj/, w64_proj/): Vitis 2025.1 drops the kernel from csim when a project is nested
# deeper.  The names are short on purpose: synthesis writes floating-point IP files about
# 150 characters below the project directory, and a path over Windows' 260-byte limit makes
# csynth fail ("Path length exceeds 260-Byte maximum").
#
# The kernel boundary (gen/poly.cpp, gen/poly.hpp) is generated; the kernel body
# (poly_body_impl.tpp, included from gen/poly.hpp) and the testbench (poly_tb.cpp) are
# hand-written.  The testbench writes each scenario's response to data/w<W>/<scenario>/<stage>/.
#
# Environment: WAVEFLOW_POLY_CLK_PERIOD_NS (default 10).

set script_dir [file dirname [file normalize [info script]]]
set stage ""
if {[info exists ::env(WAVEFLOW_POLY_STAGE)]} {
    set stage $::env(WAVEFLOW_POLY_STAGE)
}
if {$stage ni {csim synth vcd}} {
    puts "WAVEFLOW_ERROR: set WAVEFLOW_POLY_STAGE to csim, synth or vcd (got '$stage')."
    exit 1
}
set width 32
if {[info exists ::env(WAVEFLOW_POLY_WIDTH)]} {
    set width $::env(WAVEFLOW_POLY_WIDTH)
}
if {$width eq 32} {
    set top poly
} elseif {$width eq 64} {
    set top poly_bw64
} else {
    puts "WAVEFLOW_ERROR: WAVEFLOW_POLY_WIDTH must be 32 or 64 (got '$width')."
    exit 1
}
if {$stage eq "vcd"} {
    set proj vcd_proj
} else {
    set proj w${width}_proj
}

open_project -reset $proj
set_top $top
add_files gen/poly.cpp -cflags "-I."
add_files -tb poly_tb.cpp -cflags "-I. -DPOLY_WORD_BW=$width"
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
set data_dir [file join $script_dir "data" "w$width"]

if {$stage eq "csim"} {
    if {[catch {csim_design -argv "$data_dir csim"} res]} {
        puts "WAVEFLOW_ERROR: $top C simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: $top C simulation passed."
    exit 0
}

if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: $top C synthesis failed."
    puts $res
    exit 1
}
if {$stage eq "synth"} {
    set scenario timing
    set trace_level none
} else {
    set scenario early_tlast_vcd
    set trace_level port
}
if {[catch {cosim_design -argv "$data_dir cosim $scenario" -trace_level $trace_level} res]} {
    puts "WAVEFLOW_ERROR: $top RTL co-simulation of $scenario failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: $top C synthesis and RTL co-simulation of $scenario passed."
exit 0
