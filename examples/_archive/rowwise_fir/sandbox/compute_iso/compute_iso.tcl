# Vitis HLS driver for the isolated-compute toy: csim + csynth + cosim of one (nrow, ncol).
#   WAVEFLOW_CI_NROW / WAVEFLOW_CI_NCOL   (defaults 4 / 64)
set part {xc7z020clg484-1}
set nrow 4
if {[info exists ::env(WAVEFLOW_CI_NROW)]} { set nrow $::env(WAVEFLOW_CI_NROW) }
set ncol 64
if {[info exists ::env(WAVEFLOW_CI_NCOL)]} { set ncol $::env(WAVEFLOW_CI_NCOL) }
set argv "$nrow $ncol"
puts "WAVEFLOW_INFO: compute_iso nrow=$nrow ncol=$ncol"

open_project -reset compute_iso_proj
set_top compute_iso
add_files compute_iso.cpp
add_files -tb compute_iso_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: compute_iso nrow=$nrow ncol=$ncol"
exit 0
