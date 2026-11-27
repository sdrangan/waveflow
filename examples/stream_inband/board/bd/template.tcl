# PolyAccel: PS DDR through noncoherent HP0 and simple-mode AXI DMA.
# One PL-side clock domain. The framework assigns and verifies every aperture.
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 ps]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0 1 CONFIG.PSU__USE__M_AXI_GP1 0 CONFIG.PSU__USE__M_AXI_GP2 0 \
    CONFIG.PSU__USE__S_AXI_GP2 1 CONFIG.PSU__SAXIGP2__DATA_WIDTH 128 \
    CONFIG.PSU__FPGA_PL0_ENABLE 1 \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ [expr {double($WF_REQUEST_HZ) / 1e6}] \
] $ps
set control [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 control]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 2] $control
set memory [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 memory]
set_property -dict [list CONFIG.NUM_SI 2 CONFIG.NUM_MI 1] $memory
set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 dma]
set_property -dict [list \
    CONFIG.c_include_sg 0 CONFIG.c_include_mm2s 1 CONFIG.c_include_s2mm 1 \
    CONFIG.c_addr_width 32 CONFIG.c_sg_length_width 14 \
    CONFIG.c_m_axi_mm2s_data_width 32 CONFIG.c_m_axi_s2mm_data_width 32 \
    CONFIG.c_m_axis_mm2s_tdata_width 32 CONFIG.c_s_axis_s2mm_tdata_width 32 \
    CONFIG.c_include_mm2s_dre 0 CONFIG.c_include_s2mm_dre 0 \
] $dma
create_bd_cell -type ip -vlnv $WF_VLNV $WF_IP_CELL
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset
set one [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 one]
set_property -dict [list CONFIG.CONST_WIDTH 1 CONFIG.CONST_VAL 1] $one
foreach pair {
    {ps/M_AXI_HPM0_FPD control/S00_AXI}
    {control/M00_AXI poly/s_axi_control}
    {control/M01_AXI dma/S_AXI_LITE}
    {dma/M_AXI_MM2S memory/S00_AXI}
    {dma/M_AXI_S2MM memory/S01_AXI}
    {memory/M00_AXI ps/S_AXI_HP0_FPD}
    {dma/M_AXIS_MM2S poly/s_in}
    {poly/m_out dma/S_AXIS_S2MM}
} {connect_bd_intf_net [get_bd_intf_pins [lindex $pair 0]] [get_bd_intf_pins [lindex $pair 1]]}
connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/maxihpm0_fpd_aclk] \
    [get_bd_pins ps/saxihp0_fpd_aclk] [get_bd_pins control/aclk] [get_bd_pins memory/aclk] \
    [get_bd_pins reset/slowest_sync_clk] [get_bd_pins poly/ap_clk] \
    [get_bd_pins dma/s_axi_lite_aclk] [get_bd_pins dma/m_axi_mm2s_aclk] [get_bd_pins dma/m_axi_s2mm_aclk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]
connect_bd_net [get_bd_pins one/dout] [get_bd_pins reset/dcm_locked]
connect_bd_net [get_bd_pins reset/interconnect_aresetn] [get_bd_pins control/aresetn] [get_bd_pins memory/aresetn]
connect_bd_net [get_bd_pins reset/peripheral_aresetn] [get_bd_pins poly/ap_rst_n] [get_bd_pins dma/axi_resetn]
