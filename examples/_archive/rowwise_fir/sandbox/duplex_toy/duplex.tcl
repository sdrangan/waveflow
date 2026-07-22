# Vitis HLS driver for the duplex toy: csim + csynth + cosim of one (mode, N).
#   WAVEFLOW_DUPLEX_MODE  0 read-only | 1 write-only | 2 read+write   (default 2)
#   WAVEFLOW_DUPLEX_N     beats                                       (default 1024)
set part {xc7z020clg484-1}
set mode 2
if {[info exists ::env(WAVEFLOW_DUPLEX_MODE)]} { set mode $::env(WAVEFLOW_DUPLEX_MODE) }
set n 1024
if {[info exists ::env(WAVEFLOW_DUPLEX_N)]} { set n $::env(WAVEFLOW_DUPLEX_N) }
set argv "$mode $n"
puts "WAVEFLOW_INFO: duplex mode=$mode n=$n"

open_project -reset duplex_proj
set_top duplex
add_files duplex_toy.cpp
add_files -tb duplex_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: duplex mode=$mode n=$n"
exit 0
