# gather_toy_csynth.tcl — Vitis HLS C-Synthesis script for gather_toy
#
# Usage: vivado_hls -f gather_toy_csynth.tcl
# Or via waveflow: pytest tests/hw/test_gather_toy_vitis.py -m vitis

# Create new project
open_project -reset gather_toy_hls
set_top gather_toy_top

# Add source files
add_files examples/interleaver/include/gather_toy_top.cpp
add_files examples/interleaver/include/fill.h
add_files examples/interleaver/include/gather.h

# Create solution
open_solution -reset "solution1"

# Set target device (Xilinx Zynq-7000 family, representative)
set_part {xc7z020clg484-1}

# Clock period: 10ns (100MHz, typical for demo)
create_clock -period 10

# Synthesis options
config_compile -name_opt 1

# Run C-Synthesis
csynth_design

# Report results
puts "================================================"
puts "C-Synthesis Results for gather_toy"
puts "================================================"

# Get kernel metrics
set top_info [get_top_info]
puts "Top: gather_toy_top"
puts "Latency (II): [lindex $top_info 0]"
puts "Clock period: 10ns (100MHz)"

# Close project
exit 0
