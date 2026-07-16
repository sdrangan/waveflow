# gather_toy_csynth.tcl — Vitis HLS C-Synthesis script for gather_toy
#
# Synthesizes the gather_toy_top kernel with typed SOBIF interface.
# Usage: vivado_hls -f gather_toy_csynth.tcl

# Create project
open_project -reset gather_toy_hls
set_top gather_toy_top

# Add source files (relative to script directory)
set d [file dirname [file normalize [info script]]]
add_files [file join $d include gather_toy_top.cpp]

# Create solution
open_solution -reset "solution1"

# Set target device (Xilinx Zynq-7000)
set_part {xc7z020clg484-1}

# Clock period: 10ns (100MHz)
create_clock -period 10

# HLS synthesis options
config_compile -pipeline_style flp
config_compile -name_opt 1

# Run C-Synthesis
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: gather_toy csynth failed."
    puts $res
    exit 1
}

puts "WAVEFLOW_SUCCESS: gather_toy csynth passed (typed SOBIF verified)."
exit 0

