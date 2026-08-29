set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: bram_access"
open_project -reset bram_access_proj
set_top bram_access
add_files gen/bram_access.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
