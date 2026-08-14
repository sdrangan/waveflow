open_project -reset proj
set_top rx
add_files rx_kernel.cpp
open_solution -reset -flow_target vitis sol1
set_part xczu48dr-ffvg1517-2-e
create_clock -period 3.333 -name default
csynth_design
exit
