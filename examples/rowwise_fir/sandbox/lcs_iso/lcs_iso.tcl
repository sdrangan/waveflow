# Vitis HLS driver for the load->compute->store toy: csim + csynth + cosim (port trace).
#   WAVEFLOW_LCS_N / WAVEFLOW_LCS_NJ / WAVEFLOW_LCS_DEPTH   (defaults 256 / 5 / 1024)
set part {xc7z020clg484-1}
set n 256
if {[info exists ::env(WAVEFLOW_LCS_N)]}     { set n     $::env(WAVEFLOW_LCS_N) }
set nj 5
if {[info exists ::env(WAVEFLOW_LCS_NJ)]}    { set nj    $::env(WAVEFLOW_LCS_NJ) }
set depth 1024
if {[info exists ::env(WAVEFLOW_LCS_DEPTH)]} { set depth $::env(WAVEFLOW_LCS_DEPTH) }
set argv "$n $nj"
puts "WAVEFLOW_INFO: lcs n=$n nj=$nj depth=$depth"

open_project -reset lcs_iso_proj
set_top lcs_iso
add_files lcs_iso.cpp -cflags "-DLSDEPTH=$depth"
add_files -tb lcs_iso_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv" -trace_level port} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: lcs n=$n nj=$nj depth=$depth"
exit 0
