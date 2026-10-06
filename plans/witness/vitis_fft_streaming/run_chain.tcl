set inc "-IC:/Xilinx/2025.1/Vitis/tps/xf_dsp/L1/include/hw/vitis_fft/fixed -DSTREAMING -DDEEP -DCHAIN"
open_project -reset p_chain
set_top fft_top
add_files top.cpp -cflags $inc
add_files -tb tb.cpp -cflags $inc
open_solution -reset s1 -flow_target vivado
set_part {xczu48dr-ffvg1517-2-e}
create_clock -period 4
csim_design
csynth_design
cosim_design -trace_level none
exit
