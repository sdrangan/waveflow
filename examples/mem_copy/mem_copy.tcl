set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: mem_copy"
open_project -reset mem_copy_proj
set_top mem_copy
add_files gen/mem_copy.cpp -cflags $cf
add_files mem_seq_make_xfer_msg_impl.cpp -cflags $cf
add_files mem_seq_make_mr_cmd_impl.cpp -cflags $cf
add_files mem_seq_make_mw_cmd_impl.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
