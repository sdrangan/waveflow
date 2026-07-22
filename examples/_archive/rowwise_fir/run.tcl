# Vitis HLS driver for the generated free-running FIR top (Stage A).
# csim + csynth (+ optional cosim) of gen/fir.cpp (the generated ap_ctrl_hs top calling the
# fir_impl::pipeline hook in fir_pipeline_impl.tpp) against the hand-written fir_tb.cpp.
#
# Parametrized by env (vitis-run injects its own argv):
#   WAVEFLOW_FIR_SCENARIO   tb argv: single|two|three|clean|error  (default clean)
#   WAVEFLOW_FIR_COSIM      1 -> also run cosim_design               (default off)
#   WAVEFLOW_FIR_TRACE      cosim -trace_level                       (default none)
set script_dir [file dirname [file normalize [info script]]]
set part {xc7z020clg484-1}

set scenario "clean"
if {[info exists ::env(WAVEFLOW_FIR_SCENARIO)]} { set scenario $::env(WAVEFLOW_FIR_SCENARIO) }
set do_cosim 0
if {[info exists ::env(WAVEFLOW_FIR_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_FIR_COSIM) in {1 true TRUE yes YES}}]
}
set trace_level "none"
if {[info exists ::env(WAVEFLOW_FIR_TRACE)]} { set trace_level $::env(WAVEFLOW_FIR_TRACE) }

puts "WAVEFLOW_INFO: scenario=$scenario cosim=$do_cosim trace=$trace_level"

open_project -reset waveflow_fir_proj
set_top fir
add_files gen/fir.cpp -cflags "-I."
add_files -tb fir_tb.cpp -cflags "-I."
set su [file join $script_dir "include" "streamutils.cpp"]
if {[file exists $su]} { add_files -tb $su -cflags "-I." }
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$scenario"} res]} {
    puts "WAVEFLOW_ERROR: fir C-simulation failed."
    puts $res
    exit 1
}
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: fir C-synthesis failed."
    puts $res
    exit 1
}
if {$do_cosim} {
    if {[catch {cosim_design -argv "$scenario" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: fir RTL co-simulation failed."
        puts $res
        exit 1
    }
}

if {$do_cosim} {
    puts "WAVEFLOW_SUCCESS: fir C-sim, C-synth, and RTL co-sim passed (scenario=$scenario)."
} else {
    puts "WAVEFLOW_SUCCESS: fir C-sim and C-synth passed (scenario=$scenario)."
}
exit 0
