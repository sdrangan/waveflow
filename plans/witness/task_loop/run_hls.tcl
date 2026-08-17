# csynth every variant into its own project, and DO NOT stop at the first failure.
#
# `while (1)` inside a task body may well be rejected -- that is one of the three questions -- so a
# refusal has to be recorded rather than allowed to abort the run and take the other seven
# measurements with it.  Each variant is wrapped in `catch`, and its verdict is printed in a form
# report.py can read back.
#
# Same part and clock as the rest of the RF arc, so the numbers are comparable to the gates:
# xczu48dr-ffvg1517-2-e at 4.0 ns (250 MHz).

set variants {ing_1 ing_n8 ing_n64 ing_w ply_1 ply_n8 ply_n64 ply_w}

foreach top $variants {
    puts "WITNESS-BEGIN $top"
    set rc [catch {
        open_project -reset proj_$top
        set_top $top
        add_files task_loop.cpp
        open_solution -reset -flow_target vitis sol1
        set_part xczu48dr-ffvg1517-2-e
        create_clock -period 4.0 -name default
        csynth_design
    } err]
    if {$rc} {
        puts "WITNESS-RESULT $top REFUSED"
        puts "WITNESS-ERROR $top $err"
    } else {
        puts "WITNESS-RESULT $top OK"
    }
    puts "WITNESS-END $top"
}
exit
