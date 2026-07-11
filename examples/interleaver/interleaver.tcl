set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: interleaver"
open_project -reset interleaver_proj
set_top interleaver
add_files gen/interleaver.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
