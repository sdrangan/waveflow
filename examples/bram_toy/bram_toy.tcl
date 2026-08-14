set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: bram_toy"
open_project -reset bram_toy_proj
set_top bram_toy
add_files gen/bram_toy.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
