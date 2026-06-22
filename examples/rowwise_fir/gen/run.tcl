# Vitis HLS driver for the generated matrix-LT FIR top.  Mirrors the sandbox
# run.tcl conventions: WAVEFLOW_SUCCESS:/WAVEFLOW_ERROR: sentinels, env-gated
# cosim, exact m_axi depth (-D from dims.tcl) so cosim's mem model matches the TB.
set d [file dirname [file normalize [info script]]]
set data_dir [file join $d "data"]
set part {xc7z020clg484-1}
set depthflags ""
set dims [file join $data_dir "dims.tcl"]
if {[file exists $dims]} { source $dims; set depthflags " -DWF_FIR_MEM_DEPTH=$WF_FIR_MEM_DEPTH" }
set do_cosim 0
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_ROWWISE_FIR_COSIM) in {1 true TRUE yes YES}}]
}
set do_csynth 0
if {[info exists ::env(WAVEFLOW_ROWWISE_FIR_CSYNTH)]} {
    set do_csynth [expr {$::env(WAVEFLOW_ROWWISE_FIR_CSYNTH) in {1 true TRUE yes YES}}]
}
open_project -reset fir_gen_proj
set_top fir
add_files [file join $d fir.cpp] -cflags "-I$d$depthflags"
add_files -tb [file join $d fir_tb.cpp] -cflags "-I$d"
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csim_design -argv "$data_dir"} res]} { puts "WAVEFLOW_ERROR: fir csim failed."; puts $res; exit 1 }
if {$do_csynth || $do_cosim} {
    if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: fir csynth failed."; puts $res; exit 1 }
}
if {$do_cosim} {
    if {[catch {cosim_design -argv "$data_dir" -trace_level none} res]} { puts "WAVEFLOW_ERROR: fir cosim failed."; puts $res; exit 1 }
    puts "WAVEFLOW_SUCCESS: fir csim/csynth/cosim passed."
} else {
    puts "WAVEFLOW_SUCCESS: fir csim passed."
}
exit 0
