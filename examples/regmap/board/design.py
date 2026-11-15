"""Application-owned SimpFun topology, checked before implementation."""
from pathlib import Path

from waveflow.build.vivado_board import BdAddress, BdConnection, BdDesign, BdProperty


def simp_fun_design():
    return BdDesign(
        name="simp_fun", template=Path(__file__).parent / "bd/template.tcl",
        ip_cell="simp_fun", clock_pin="ps/pl_clk0",
        # This application reserves one 64 KiB PS aperture for AXI-Lite control.
        addresses=(BdAddress("ps/Data", "simp_fun/s_axi_control/Reg", 0xA1000000, 0x10000),),
        connections=(
            BdConnection("interface", ("ps/M_AXI_HPM0_FPD", "axi/S00_AXI")),
            BdConnection("interface", ("axi/M00_AXI", "simp_fun/s_axi_control")),
            BdConnection("net", ("ps/pl_clk0", "ps/maxihpm0_fpd_aclk", "axi/aclk",
                                 "reset/slowest_sync_clk", "simp_fun/ap_clk")),
            BdConnection("net", ("ps/pl_resetn0", "reset/ext_reset_in")),
            BdConnection("net", ("one/dout", "reset/dcm_locked")),
            BdConnection("net", ("reset/interconnect_aresetn", "axi/aresetn")),
            BdConnection("net", ("reset/peripheral_aresetn", "simp_fun/ap_rst_n")),
        ),
        properties=(
            BdProperty("cell", "ps", "VLNV", "xilinx.com:ip:zynq_ultra_ps_e:3.5"),
            BdProperty("cell", "axi", "VLNV", "xilinx.com:ip:smartconnect:1.0"),
            BdProperty("cell", "reset", "VLNV", "xilinx.com:ip:proc_sys_reset:5.0"),
            BdProperty("cell", "one", "CONFIG.CONST_VAL", "1"),
            BdProperty("cell", "reset", "CONFIG.C_EXT_RESET_HIGH", "0"),
            BdProperty("pin", "simp_fun/ap_rst_n", "CONFIG.POLARITY", "ACTIVE_LOW"),
        ),
    )
