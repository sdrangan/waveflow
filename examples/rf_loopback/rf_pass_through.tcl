set part {xczu48dr-ffvg1517-2-e}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: rf_pass_through"
open_project -reset rf_pass_through_proj
set_top rf_pass_through
add_files gen/rf_pass_through.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 4
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
