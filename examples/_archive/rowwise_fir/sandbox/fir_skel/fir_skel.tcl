# Vitis HLS driver for the real-FIR-compute skeleton: csim + csynth + cosim (port trace).
#   WAVEFLOW_FS_NROW / WAVEFLOW_FS_NCOL / WAVEFLOW_FS_NJ / WAVEFLOW_FS_DEPTH  (4 / 64 / 6 / 2048)
set part {xc7z020clg484-1}
set nr 4
if {[info exists ::env(WAVEFLOW_FS_NROW)]}  { set nr    $::env(WAVEFLOW_FS_NROW) }
set nc 64
if {[info exists ::env(WAVEFLOW_FS_NCOL)]}  { set nc    $::env(WAVEFLOW_FS_NCOL) }
set nj 6
if {[info exists ::env(WAVEFLOW_FS_NJ)]}    { set nj    $::env(WAVEFLOW_FS_NJ) }
set depth 2048
if {[info exists ::env(WAVEFLOW_FS_DEPTH)]} { set depth $::env(WAVEFLOW_FS_DEPTH) }
set cflags "-DLSDEPTH=$depth"
if {[info exists ::env(WAVEFLOW_FS_BUF)] && $::env(WAVEFLOW_FS_BUF) == "1"} {
    set cflags "$cflags -DFS_BUF"     ;# 2-pass buffered slice (the framework read/write_array_slice path)
}
set argv "$nr $nc $nj"
puts "WAVEFLOW_INFO: fir_skel nr=$nr nc=$nc nj=$nj depth=$depth cflags=$cflags"

open_project -reset fir_skel_proj
set_top fir_skel
add_files fir_skel.cpp -cflags "$cflags"
add_files -tb fir_skel_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv" -trace_level port} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: fir_skel nr=$nr nc=$nc nj=$nj depth=$depth"
exit 0
