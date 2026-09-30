open_project -reset probe_proj
set_top probe_cmac
add_files probe.cpp
open_solution -reset solution1
set_part {xczu48dr-ffvg1517-2-e}
create_clock -period 4
if {[catch {csynth_design} res]} { puts "PROBE_ERROR: $res"; exit 1 }
puts "PROBE_CSYNTH_OK"
exit 0
