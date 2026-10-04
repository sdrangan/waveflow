set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: markov_gen"
open_project -reset markov_gen_proj
set_top markov_gen
add_files gen/markov_gen.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
