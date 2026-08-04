# Vitis HLS C-simulation of the VecMult task body against the pysim golden.
#
# Separate from the generated vec_mult.tcl (which is csynth-only) because the generated TCL has no
# testbench hook.  csim compiles the task body plus vec_mult_tb.cpp and runs main(); it never
# touches the ap_ctrl_none top, which would spin forever.
set script_dir [file dirname [file normalize [info script]]]
set cf "-Iinclude"

open_project -reset vec_mult_csim_proj
set_top vec_mult
add_files gen/vec_mult.cpp -cflags $cf
add_files -tb vec_mult_tb.cpp -cflags $cf
open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
if {[catch {csim_design} res]} { puts "WAVEFLOW_ERROR: csim"; puts $res; exit 1 }
puts "WAVEFLOW_CSIM_DONE"
exit 0
