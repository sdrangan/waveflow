# run.tcl -- C-simulation, C-synthesis and C/RTL co-simulation of the SSR FFT DUT.
#
# Driven by vitis-run:
#   vitis-run --mode hls --tcl run.tcl
#
# The Vitis DSP library headers are found via the WF_VITIS_LIBS environment variable; it must
# point at .../dsp/L1/include/hw/vitis_fft/fixed  (see README.md).
set here [file dirname [file normalize [info script]]]

if {[info exists ::env(WF_VITIS_LIBS)]} {
    set vlib $::env(WF_VITIS_LIBS)
} else {
    puts "WAVEFLOW_ERROR: set WF_VITIS_LIBS to .../dsp/L1/include/hw/vitis_fft/fixed"
    exit 1
}
set cf "-I$here/src -I$vlib -std=c++14"
# WF_INPUT selects which file under data/ to drive the DUT with, so checking your own samples
# does not mean overwriting the shipped vectors.  See "Bring your own input" in README.md.
set inname "input.txt"
if {[info exists ::env(WF_INPUT)]} { set inname $::env(WF_INPUT) }
set data "$here/data/$inname"
if {![file exists $data]} {
    puts "WAVEFLOW_ERROR: $data does not exist (WF_INPUT=$inname)"
    exit 1
}
puts "WAVEFLOW_INPUT: $inname"

open_project -reset fft_verify_proj
set_top fft_top
add_files    $here/src/fft_top.cpp -cflags $cf
add_files -tb $here/src/fft_tb.cpp -cflags $cf

open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10

# ---- 1. C-simulation: the generated C++ running natively ----
if {[catch {csim_design -argv "$data $here/results/output_csim.txt"} res]} {
    puts "WAVEFLOW_ERROR: csim failed"; puts $res; exit 1
}
puts "WAVEFLOW_CSIM_OK"

# ---- 2. C-synthesis: C++ -> RTL ----
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: csynth failed"; puts $res; exit 1
}
puts "WAVEFLOW_CSYNTH_OK"

# ---- 3. C/RTL co-simulation: the SAME testbench driving the synthesized RTL in xsim.
#         This is the RTL simulation -- Vitis has no separate RTL-sim step for an
#         ap_ctrl_hs kernel like this one. ----
if {[catch {cosim_design -argv "$data $here/results/output_cosim.txt" -trace_level none} res]} {
    puts "WAVEFLOW_ERROR: cosim failed"; puts $res; exit 1
}
puts "WAVEFLOW_COSIM_OK"

puts "WAVEFLOW_SUCCESS: csim + csynth + cosim all passed"
exit 0
