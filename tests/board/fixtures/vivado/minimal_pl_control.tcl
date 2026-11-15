# S0d standalone PL-clock shell; no SimpFun, DMA, DDR master, or RFDC.
# RFSoC4x2 reference: Ruibin2000/DDR_BRAM_tx_rx_v1, commit
# 84275a9e1dd178fcbe6e2c892fbb32e26c1d480e, vivado/constraints.xdc.
# The board LMK must supply 122.88 MHz on AN11/AP11 before hardware use.
# This template assumes that input; implementation is not a board-clock measurement.
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 ps]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0 1 \
    CONFIG.PSU__USE__M_AXI_GP1 0 \
    CONFIG.PSU__USE__M_AXI_GP2 0 \
    CONFIG.PSU__FPGA_PL0_ENABLE 1 \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100.0 \
] $ps
# PS PL0 is unused. The external PL MMCM clocks the HPM AXI interface too.
set cw [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 cw]
set_property -dict [list \
    CONFIG.PRIMITIVE MMCM \
    CONFIG.PRIM_SOURCE Differential_clock_capable_pin \
    CONFIG.PRIM_IN_FREQ 122.880 \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [expr {double($WF_REQUEST_HZ) / 1e6}] \
    CONFIG.USE_RESET false \
] $cw
set reference [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:diff_clock_rtl:1.0 fpga_refclk_in]
set_property CONFIG.FREQ_HZ 122880000 $reference
connect_bd_intf_net $reference [get_bd_intf_pins cw/CLK_IN1_D]
# The generic connection contract handles pins, so check this external port explicitly.
wf_equal [get_bd_intf_nets -of_objects $reference] \
    [get_bd_intf_nets -of_objects [get_bd_intf_pins cw/CLK_IN1_D]] pl_reference_connection
wf_equal [get_property CONFIG.FREQ_HZ $reference] 122880000 pl_reference_frequency

set sc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 1] $sc
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset
create_bd_cell -type ip -vlnv $WF_VLNV $WF_IP_CELL
connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_HPM0_FPD] [get_bd_intf_pins axi/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi/M00_AXI] [get_bd_intf_pins $WF_IP_CELL/s_axi_control]
connect_bd_net [get_bd_pins cw/clk_out1] [get_bd_pins ps/maxihpm0_fpd_aclk] \
    [get_bd_pins axi/aclk] [get_bd_pins reset/slowest_sync_clk] [get_bd_pins $WF_IP_CELL/ap_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]
connect_bd_net [get_bd_pins cw/locked] [get_bd_pins reset/dcm_locked]
connect_bd_net [get_bd_pins reset/interconnect_aresetn] [get_bd_pins axi/aresetn]
connect_bd_net [get_bd_pins reset/peripheral_aresetn] [get_bd_pins $WF_IP_CELL/ap_rst_n]

# Materialize fixture-owned pin constraints from this hashed template. Do not
# modify the immutable sources/ bundle. Clock Wizard supplies the input/generated
# clock timing constraints; do not duplicate them with another create_clock.
file mkdir generated_constraints
set xdc [open generated_constraints/pl_reference.xdc w]
puts $xdc {set_property PACKAGE_PIN AN11 [get_ports fpga_refclk_in_clk_p]}
puts $xdc {set_property PACKAGE_PIN AP11 [get_ports fpga_refclk_in_clk_n]}
puts $xdc {set_property IOSTANDARD LVDS [get_ports {fpga_refclk_in_clk_p fpga_refclk_in_clk_n}]}
close $xdc
add_files -fileset constrs_1 -norecurse generated_constraints/pl_reference.xdc
