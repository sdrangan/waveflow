open_project -reset p
set_top sweep
add_files sweep.cpp
open_solution -reset s
set_part xczu48dr-ffvg1517-2-e
create_clock -period 4
csynth_design
exit
