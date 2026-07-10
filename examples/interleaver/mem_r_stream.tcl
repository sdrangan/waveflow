set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: mem_r_stream"
open_project -reset mem_r_stream_proj
set_top mem_r_stream
add_files gen/mem_r_stream.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
