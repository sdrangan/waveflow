set part {xc7z020clg484-1}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: rf_samp_buf_rx"
open_project -reset rf_samp_buf_rx_proj
set_top rf_samp_buf_rx
add_files gen/rf_samp_buf_rx.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
