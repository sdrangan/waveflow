open_project -reset waveflow_arrayutils_slice_ii_proj
set_top slice_ii_top
add_files arrayutils_slice_ii_test.cpp

set script_dir [file dirname [file normalize [info script]]]
set streamutils_cpp [file join $script_dir "streamutils.cpp"]
if {![file exists $streamutils_cpp]} {
    set streamutils_cpp [file join $script_dir "include" "streamutils.cpp"]
}
if {[file exists $streamutils_cpp]} {
    add_files $streamutils_cpp
}

open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10

if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: HLS C-Synthesis failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: Arrayutils slice II csynth passed."
exit 0
