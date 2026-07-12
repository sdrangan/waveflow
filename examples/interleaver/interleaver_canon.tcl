set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: interleaver_canon"
open_project -reset interleaver_canon_proj
set_top interleaver_canon
add_files gen/interleaver_canon.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
