open_project -reset p
set_top sob_bank
add_files sob_bank.cpp
open_solution -reset s
set_part xczu48dr-ffvg1517-2-e
create_clock -period 4
csynth_design
exit
