set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: state_accum"
open_project -reset state_accum_proj
set_top state_accum
add_files gen/state_accum.cpp -cflags $cf
add_files state_accum_accumulate_impl.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
