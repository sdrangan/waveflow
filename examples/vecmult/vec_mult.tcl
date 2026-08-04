set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: vec_mult"
open_project -reset vec_mult_proj
set_top vec_mult
add_files gen/vec_mult.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
