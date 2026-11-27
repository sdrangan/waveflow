"""Independent structural intent for the isolated S0d fixture."""
from pathlib import Path

from waveflow.build.vivado_board import BdAddress, BdConnection, BdDesign, BdProperty


def minimal_design(template=None):
    return BdDesign(
        name="s0d_control", template=template or Path(__file__).parent / "fixtures/vivado/minimal_ps_control.tcl",
        ip_cell="probe", clock_pin="ps/pl_clk0",
        # An explicit fixture assignment, not the reference application's copied address map.
        addresses=(BdAddress("ps/Data", "probe/s_axi_control/Reg", 0xA1000000, 0x10000),),
        connections=(
            BdConnection("interface", ("ps/M_AXI_HPM0_FPD", "axi/S00_AXI")),
            BdConnection("interface", ("axi/M00_AXI", "probe/s_axi_control")),
            BdConnection("net", ("ps/pl_clk0", "ps/maxihpm0_fpd_aclk", "axi/aclk",
                                 "reset/slowest_sync_clk", "probe/ap_clk")),
            BdConnection("net", ("ps/pl_resetn0", "reset/ext_reset_in")),
            BdConnection("net", ("one/dout", "reset/dcm_locked")),
            BdConnection("net", ("reset/interconnect_aresetn", "axi/aresetn")),
            BdConnection("net", ("reset/peripheral_aresetn", "probe/ap_rst_n")),
        ),
        properties=(
            BdProperty("cell", "ps", "VLNV", "xilinx.com:ip:zynq_ultra_ps_e:3.5"),
            BdProperty("cell", "axi", "VLNV", "xilinx.com:ip:smartconnect:1.0"),
            BdProperty("cell", "reset", "VLNV", "xilinx.com:ip:proc_sys_reset:5.0"),
            BdProperty("cell", "one", "CONFIG.CONST_VAL", "1"),
            BdProperty("cell", "reset", "CONFIG.C_EXT_RESET_HIGH", "0"),
            BdProperty("pin", "probe/ap_rst_n", "CONFIG.POLARITY", "ACTIVE_LOW"),
        ),
    )
