# Vitis HLS driver for the block_scale example: the generated m_axi kernel
# (gen/block_scale.cpp) + generated testbench (gen/block_scale_tb.cpp), compiled
# against the hand-written pure-compute hook (block_scale_compute_impl.cpp).
# All C++ is generated from block_scale.py; this drives csim / csynth / cosim.
open_project -reset waveflow_block_scale_proj
set_top block_scale
add_files gen/block_scale.cpp -cflags "-I. -Igen"
add_files block_scale_compute_impl.cpp -cflags "-I. -Igen"
add_files -tb gen/block_scale_tb.cpp -cflags "-I. -Igen"

set script_dir [file dirname [file normalize [info script]]]
set streamutils_cpp [file join $script_dir "include" "streamutils.cpp"]
if {[file exists $streamutils_cpp]} {
    add_files -tb $streamutils_cpp -cflags "-I. -Igen"
}

open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
set data_dir [file join $script_dir "data"]

set do_cosim 0
set trace_level "none"
if {[info exists ::env(WAVEFLOW_BLOCK_SCALE_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_BLOCK_SCALE_COSIM) in {1 true TRUE yes YES}}]
}
if {[info exists ::env(WAVEFLOW_BLOCK_SCALE_TRACE_LEVEL)]} {
    set trace_level $::env(WAVEFLOW_BLOCK_SCALE_TRACE_LEVEL)
}

if {[catch {csim_design -argv "$data_dir"} res]} {
    puts "WAVEFLOW_ERROR: block_scale C-simulation failed."
    puts $res
    exit 1
}

if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: block_scale C-synthesis failed."
    puts $res
    exit 1
}

if {$do_cosim} {
    if {[catch {cosim_design -argv "$data_dir" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: block_scale RTL co-simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: block_scale C-sim, C-synth, and RTL co-sim passed."
} else {
    puts "WAVEFLOW_SUCCESS: block_scale C-sim and C-synth passed."
}
exit 0
