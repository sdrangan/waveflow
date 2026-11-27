proc wf_equal {actual expected label} {
    if {$actual ne $expected} {error "$label mismatch: $actual != $expected"}
}
proc wf_connected {kind pins} {
    set getter get_bd_pins
    set nets get_bd_nets
    if {$kind eq "interface"} {set getter get_bd_intf_pins; set nets get_bd_intf_nets}
    set first ""
    foreach pin $pins {
        set obj [$getter -quiet $pin]
        if {[llength $obj] != 1} {error "missing or ambiguous pin: $pin"}
        set net [$nets -quiet -of_objects $obj]
        if {[llength $net] != 1} {error "unconnected pin: $pin"}
        if {$first eq ""} {set first $net} else {wf_equal $net $first "connection $pin"}
    }
}
# S0d project-relative build. Never reset or overwrite an existing project.
set WF_ROOT [file dirname [file normalize [info script]]]
cd $WF_ROOT
set WF_BD {s0d_control}
set WF_VLNV {waveflow:hls:scalar_probe:1.0}
set WF_IP_CELL {probe}
set WF_REQUEST_HZ 100000000
if {[file exists project]} {error "refusing to overwrite existing project"}
set_param general.maxThreads 2
set_param board.repoPaths [list [file normalize sources/boards]]
if {[llength [get_board_parts -quiet {realdigital.org:rfsoc4x2:part0:1.0}]] != 1} {error "board part unavailable"}
create_project board_build project -part {xczu48dr-ffvg1517-2-e}
set_property board_part {realdigital.org:rfsoc4x2:part0:1.0} [current_project]
set_property ip_repo_paths [list [file normalize sources/ip]] [current_project]
update_ip_catalog
if {[llength [get_ipdefs -all $WF_VLNV]] != 1} {error "IP VLNV unavailable or ambiguous"}
create_bd_design $WF_BD
source sources/template.tcl
# Explicit address assignments; do not import the reference application map.
if {[llength [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces {ps/Data}]]] != 0} {error "template must not preassign the declared address space"}
assign_bd_address -offset 0xA1000000 -range 0x10000 -target_address_space [get_bd_addr_spaces {ps/Data}] [get_bd_addr_segs {probe/s_axi_control/Reg}]
validate_bd_design
save_bd_design
wf_equal [get_property PART [current_project]] {xczu48dr-ffvg1517-2-e} part
wf_equal [get_property BOARD_PART [current_project]] {realdigital.org:rfsoc4x2:part0:1.0} board_part
wf_equal [get_property VLNV [get_bd_cells $WF_IP_CELL]] $WF_VLNV ip_vlnv
wf_equal [get_property {VLNV} [get_bd_cells {ps}]] {xilinx.com:ip:zynq_ultra_ps_e:3.5} {ps:VLNV}
wf_equal [get_property {VLNV} [get_bd_cells {axi}]] {xilinx.com:ip:smartconnect:1.0} {axi:VLNV}
wf_equal [get_property {VLNV} [get_bd_cells {reset}]] {xilinx.com:ip:proc_sys_reset:5.0} {reset:VLNV}
wf_equal [get_property {CONFIG.CONST_VAL} [get_bd_cells {one}]] {1} {one:CONFIG.CONST_VAL}
wf_equal [get_property {CONFIG.C_EXT_RESET_HIGH} [get_bd_cells {reset}]] {0} {reset:CONFIG.C_EXT_RESET_HIGH}
wf_equal [get_property {CONFIG.POLARITY} [get_bd_pins {probe/ap_rst_n}]] {ACTIVE_LOW} {probe/ap_rst_n:CONFIG.POLARITY}
wf_connected interface [list {ps/M_AXI_HPM0_FPD} {axi/S00_AXI}]
wf_connected interface [list {axi/M00_AXI} {probe/s_axi_control}]
wf_connected net [list {ps/pl_clk0} {ps/maxihpm0_fpd_aclk} {axi/aclk} {reset/slowest_sync_clk} {probe/ap_clk}]
wf_connected net [list {ps/pl_resetn0} {reset/ext_reset_in}]
wf_connected net [list {one/dout} {reset/dcm_locked}]
wf_connected net [list {reset/interconnect_aresetn} {axi/aresetn}]
wf_connected net [list {reset/peripheral_aresetn} {probe/ap_rst_n}]
set actual_hz [get_property CONFIG.FREQ_HZ [get_bd_pins {ps/pl_clk0}]]
if {![string is double -strict $actual_hz] || $actual_hz <= 0 || abs(double($actual_hz) - 100000000) / 100000000 * 1e6 > 1} {error "physical clock differs from requested profile"}
set verdict [open bd_verdict.tsv w]
puts $verdict "clock_hz	$actual_hz"
foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces {ps/Data}]] {
    set target_seg [get_bd_addr_segs -of_objects $seg]
    puts $verdict "address\tps/Data\t$target_seg\t[get_property OFFSET $seg]\t[get_property RANGE $seg]"
}
close $verdict
foreach run [get_runs] {wf_equal [get_property PROGRESS $run] 0% no_synthesis}
write_bd_tcl -include_layout validated_bd.tcl
save_bd_design
