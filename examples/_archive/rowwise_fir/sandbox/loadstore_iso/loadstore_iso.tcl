# Vitis HLS driver for the load‖store toy: csim + csynth + cosim (port trace) of NJOBS×N.
#   WAVEFLOW_LS_N / WAVEFLOW_LS_NJ   (defaults 256 / 3)
set part {xc7z020clg484-1}
set n 256
if {[info exists ::env(WAVEFLOW_LS_N)]}  { set n  $::env(WAVEFLOW_LS_N) }
set nj 3
if {[info exists ::env(WAVEFLOW_LS_NJ)]} { set nj $::env(WAVEFLOW_LS_NJ) }
set argv "$n $nj"
puts "WAVEFLOW_INFO: loadstore n=$n nj=$nj"

open_project -reset loadstore_iso_proj
set_top loadstore_iso
add_files loadstore_iso.cpp
add_files -tb loadstore_iso_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv" -trace_level port} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: loadstore n=$n nj=$nj"
exit 0
