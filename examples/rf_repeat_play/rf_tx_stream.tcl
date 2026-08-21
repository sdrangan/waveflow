set part {xczu48dr-ffvg1517-2-e}
set cf "-Iinclude"
puts "WAVEFLOW_INFO: rf_tx_stream"
open_project -reset rf_tx_stream_proj
set_top rf_tx_stream
add_files gen/rf_tx_stream.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 4
config_rtl -reset state
if {[catch {csynth_design} res]} { puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }
puts "WAVEFLOW_CSYNTH_OK"
exit 0
