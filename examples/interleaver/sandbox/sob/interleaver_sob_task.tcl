set part {xc7z020clg484-1}
open_project -reset sob_task_proj
set_top interleaver_sob_task
add_files interleaver_sob_task.cpp
open_solution -reset "solution1"
set_part $part
create_clock -period 10
# csynth only -> export RTL for XSI (Vitis cosim is unreliable for ap_ctrl_none free-running tasks).
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
