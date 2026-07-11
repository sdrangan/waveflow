set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: sob_toy"
open_project -reset sob_toy_proj
set_top sob_toy
add_files gen/sob_toy.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
