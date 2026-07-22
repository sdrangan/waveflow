# Vitis HLS driver for the half-pipe toys: WAVEFLOW_HP_MODE = lc | cs.
#   lc = real m_axi load + compute + fake store ;  cs = fake BRAM load + compute + real m_axi store
#   WAVEFLOW_HP_N / WAVEFLOW_HP_NJ / WAVEFLOW_HP_DEPTH   (defaults 256 / 6 / 1024)
set part {xc7z020clg484-1}
set mode "lc"
if {[info exists ::env(WAVEFLOW_HP_MODE)]}  { set mode  $::env(WAVEFLOW_HP_MODE) }
set n 256
if {[info exists ::env(WAVEFLOW_HP_N)]}     { set n     $::env(WAVEFLOW_HP_N) }
set nj 6
if {[info exists ::env(WAVEFLOW_HP_NJ)]}    { set nj    $::env(WAVEFLOW_HP_NJ) }
set depth 1024
if {[info exists ::env(WAVEFLOW_HP_DEPTH)]} { set depth $::env(WAVEFLOW_HP_DEPTH) }
set argv "$n $nj"
puts "WAVEFLOW_INFO: halfpipe mode=$mode n=$n nj=$nj depth=$depth"

open_project -reset ${mode}_iso_proj
set_top ${mode}_iso
add_files ${mode}_iso.cpp -cflags "-DLSDEPTH=$depth"
add_files -tb ${mode}_iso_tb.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10

if {[catch {csim_design -argv "$argv"} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
if {[catch {cosim_design -argv "$argv" -trace_level port} res]} { puts "WAVEFLOW_ERROR: cosim"; puts $res; exit 1 }
puts "WAVEFLOW_SUCCESS: halfpipe mode=$mode n=$n nj=$nj depth=$depth"
exit 0
