# Verify that the vendored board repository is visible without changing user-global settings.
proc waveflow_fail {message} {
    puts stderr "WAVEFLOW_ERROR: $message"
    exit 1
}

if {$argc != 3} {
    waveflow_fail "expected arguments: <board_repo_dir> <board_part> <device_part>"
}

set board_repo_dir [file normalize [lindex $argv 0]]
set expected_board_part [lindex $argv 1]
set expected_device_part [lindex $argv 2]

set_param board.repoPaths [list $board_repo_dir]
set board_matches [get_board_parts -quiet $expected_board_part]
if {[llength $board_matches] != 1} {
    waveflow_fail "board part '$expected_board_part' was not discovered under '$board_repo_dir'"
}

set part_matches [get_parts -quiet $expected_device_part]
if {[llength $part_matches] != 1} {
    waveflow_fail "device part '$expected_device_part' is unavailable"
}

puts "WAVEFLOW_BOARD_OK: $expected_board_part -> $expected_device_part"
exit 0
