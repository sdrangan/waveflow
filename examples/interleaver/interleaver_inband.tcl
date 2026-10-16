set part {xc7z020clg484-1}
set cf "-Isrc -Iinclude"
puts "WAVEFLOW_INFO: interleaver_inband"
open_project -reset interleaver_inband_proj
set_top interleaver_inband
add_files gen/interleaver_inband.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
