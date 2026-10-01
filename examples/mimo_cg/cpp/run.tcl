# Vitis HLS C-simulation driver for one CG conformance case set (plans/mimo_cg, step 2.4).
# mimo_cg_conformance.py writes main.cpp, cg_ref.h and inputs.txt next to this script; csim
# writes outputs.txt back here.  The part is the study's RFSoC 4x2 target.
open_project -reset cg_conf_proj
set_top main
add_files -tb main.cpp -cflags "-I."

open_solution -reset "solution1"
set_part {xczu48dr-ffvg1517-2-e}
create_clock -period 4

set d [file dirname [file normalize [info script]]]
set argv_paths "[file join $d inputs.txt] [file join $d outputs.txt]"

if {[catch {csim_design -argv $argv_paths} res]} {
    puts "WAVEFLOW_ERROR: HLS C-Simulation failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: CG conformance csim passed."
exit 0
