set part {xc7z020clg484-1}
set cf "-Isrc -Iinclude"
puts "WAVEFLOW_INFO: mm_fir"
open_project -reset mm_fir_proj
set_top mm_fir
add_files gen/mm_fir.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
