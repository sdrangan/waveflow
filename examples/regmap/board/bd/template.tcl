# SimpFun: RFSoC4x2 PS -> SmartConnect -> AXI-Lite control.
# One PS PL clock, active-low peripheral reset, explicit address assignment.
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 ps]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0 1 \
    CONFIG.PSU__USE__M_AXI_GP1 0 \
    CONFIG.PSU__USE__M_AXI_GP2 0 \
    CONFIG.PSU__FPGA_PL0_ENABLE 1 \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ [expr {double($WF_REQUEST_HZ) / 1e6}] \
] $ps
set sc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 1] $sc
set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset]
# Reset polarity is inferred from the connected active-low PS pin; checked after propagation.
set one [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 one]
set_property -dict [list CONFIG.CONST_WIDTH 1 CONFIG.CONST_VAL 1] $one
create_bd_cell -type ip -vlnv $WF_VLNV $WF_IP_CELL
connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_HPM0_FPD] [get_bd_intf_pins axi/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi/M00_AXI] [get_bd_intf_pins $WF_IP_CELL/s_axi_control]
connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/maxihpm0_fpd_aclk] \
    [get_bd_pins axi/aclk] [get_bd_pins reset/slowest_sync_clk] [get_bd_pins $WF_IP_CELL/ap_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]
connect_bd_net [get_bd_pins one/dout] [get_bd_pins reset/dcm_locked]
connect_bd_net [get_bd_pins reset/interconnect_aresetn] [get_bd_pins axi/aresetn]
connect_bd_net [get_bd_pins reset/peripheral_aresetn] [get_bd_pins $WF_IP_CELL/ap_rst_n]
