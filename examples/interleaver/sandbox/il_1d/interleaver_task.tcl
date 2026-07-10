set part {xc7z020clg484-1}
set n 256;  if {[info exists ::env(WAVEFLOW_IL_N)]}  { set n  $::env(WAVEFLOW_IL_N) }
set nj 4;   if {[info exists ::env(WAVEFLOW_IL_NJ)]} { set nj $::env(WAVEFLOW_IL_NJ) }
set dw 64;  if {[info exists ::env(WAVEFLOW_IL_DW)]} { set dw $::env(WAVEFLOW_IL_DW) }
set cf "-Iinclude -DMEM_DW=$dw"
puts "WAVEFLOW_INFO: interleaver_task n=$n nj=$nj MEM_DW=$dw"
open_project -reset interleaver_task_proj
set_top interleaver
add_files interleaver_task.cpp -cflags $cf
add_files -tb interleaver_task_tb.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csim_design -argv "$n $nj"} res]}   { puts "WAVEFLOW_ERROR: csim";   puts $res; exit 1 }
puts "WAVEFLOW_CSIM_OK"
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
