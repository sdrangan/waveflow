set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: mm_queue_writer_64_128"
open_project -reset mm_queue_writer_64_128_proj
set_top mm_queue_writer_64_128
add_files gen/mm_queue_writer_64_128.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
