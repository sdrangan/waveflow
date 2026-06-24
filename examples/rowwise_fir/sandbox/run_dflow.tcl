# Vitis HLS driver for the control-driven DATAFLOW FIR sandbox
# (plans/fir-cleanup.md §1 Phase 1).  Builds ONE solution (single shared m_axi
# bundle) and runs csim + csynth, plus optional RTL cosim.
#
# Parametrized by environment (vitis-run injects its own argv, so we use env):
#   WAVEFLOW_FIR_DFLOW_SCENARIO   tb argv: single|two|three|clean|error  (default clean)
#   WAVEFLOW_FIR_DFLOW_COSIM      1 -> also run cosim_design                (default off)
#   WAVEFLOW_FIR_DFLOW_TRACE      cosim -trace_level (none|all|port)        (default none)
#
# Conventions mirror examples/rowwise_fir/sandbox/run.tcl: WAVEFLOW_SUCCESS: /
# WAVEFLOW_ERROR: sentinel lines and exit 0/1.
set script_dir [file dirname [file normalize [info script]]]
set part {xc7z020clg484-1}

set scenario "clean"
if {[info exists ::env(WAVEFLOW_FIR_DFLOW_SCENARIO)]} {
    set scenario $::env(WAVEFLOW_FIR_DFLOW_SCENARIO)
}
set do_cosim 0
if {[info exists ::env(WAVEFLOW_FIR_DFLOW_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_FIR_DFLOW_COSIM) in {1 true TRUE yes YES}}]
}
set trace_level "none"
if {[info exists ::env(WAVEFLOW_FIR_DFLOW_TRACE)]} {
    set trace_level $::env(WAVEFLOW_FIR_DFLOW_TRACE)
}

puts "WAVEFLOW_INFO: scenario=$scenario cosim=$do_cosim trace=$trace_level"

open_project -reset waveflow_fir_dflow_proj
set_top fir
add_files [file join $script_dir fir_dflow_sandbox.cpp] -cflags "-I$script_dir"
add_files -tb [file join $script_dir fir_dflow_tb.cpp] -cflags "-I$script_dir"
open_solution -reset "solution_dflow"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$scenario"} res]} {
    puts "WAVEFLOW_ERROR: fir_dflow C-simulation failed."
    puts $res
    exit 1
}
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: fir_dflow C-synthesis failed."
    puts $res
    exit 1
}
if {$do_cosim} {
    if {[catch {cosim_design -argv "$scenario" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: fir_dflow RTL co-simulation failed."
        puts $res
        exit 1
    }
}

if {$do_cosim} {
    puts "WAVEFLOW_SUCCESS: fir_dflow C-sim, C-synth, and RTL co-sim passed (scenario=$scenario)."
} else {
    puts "WAVEFLOW_SUCCESS: fir_dflow C-sim and C-synth passed (scenario=$scenario)."
}
exit 0
