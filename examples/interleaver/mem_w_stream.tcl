set part {xc7z020clg484-1}
set dw 64; if {[info exists ::env(WAVEFLOW_IL_DW)]} { set dw $::env(WAVEFLOW_IL_DW) }
set cf "-Iinclude -DMEM_DW=$dw"
puts "WAVEFLOW_INFO: mem_w_stream MEM_DW=$dw"
open_project -reset mem_w_stream_proj
set_top mem_w_stream
add_files gen/mem_w_stream.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
