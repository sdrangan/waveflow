# Run a minimal synthesis so license/device-install failures are known before a bitstream build.
proc waveflow_fail {message} {
    puts stderr "WAVEFLOW_ERROR: $message"
    exit 1
}

if {$argc != 1} {
    waveflow_fail "expected argument: <device_part>"
}

set device_part [lindex $argv 0]
set probe_source [file join [pwd] waveflow_license_probe.v]

if {[catch {
    set source_handle [open $probe_source w]
    puts $source_handle {module waveflow_license_probe(input wire i, output wire o); assign o = i; endmodule}
    close $source_handle
    create_project -in_memory -part $device_part waveflow_license_probe
    read_verilog $probe_source
    synth_design -top waveflow_license_probe -part $device_part
} failure]} {
    catch {close $source_handle}
    waveflow_fail "xczu48dr synthesis/license probe failed: $failure"
}

puts "WAVEFLOW_SYNTH_LICENSE_OK: $device_part"
exit 0
