# Vitis HLS driver for the m_axi read-form isolation: csim + csynth + cosim of one READ_MODE.
#   WAVEFLOW_MR_MODE  0..4   (see mem_read_iso.cpp)     WAVEFLOW_MR_N / WAVEFLOW_MR_NJ  (64 / 3)
set part {xc7z020clg484-1}
set mode 0
if {[info exists ::env(WAVEFLOW_MR_MODE)]} { set mode $::env(WAVEFLOW_MR_MODE) }
set n 64
if {[info exists ::env(WAVEFLOW_MR_N)]}    { set n    $::env(WAVEFLOW_MR_N) }
set nj 3
if {[info exists ::env(WAVEFLOW_MR_NJ)]}   { set nj   $::env(WAVEFLOW_MR_NJ) }
set argv "$n $nj"
puts "WAVEFLOW_INFO: mem_read_iso mode=$mode n=$n nj=$nj"

open_project -reset mem_read_iso_proj
set_top mem_read_iso
add_files mem_read_iso.cpp -cflags "-DREAD_MODE=$mode"
add_files -tb mem_read_iso_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: mem_read_iso mode=$mode n=$n nj=$nj"
exit 0
