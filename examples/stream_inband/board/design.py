"""Application-owned 100 MHz, noncoherent PS DDR / AXI DMA topology."""
from pathlib import Path

from waveflow.build.vivado_board import BdAddress, BdConnection, BdDesign, BdProperty


def poly_design():
    return BdDesign(
        name="poly", template=Path(__file__).parent / "bd/template.tcl",
        ip_cell="poly", clock_pin="ps/pl_clk0",
        addresses=(
            BdAddress("ps/Data", "poly/s_axi_control/Reg", 0xA1000000, 0x10000),
            BdAddress("ps/Data", "dma/S_AXI_LITE/Reg", 0xA1010000, 0x10000),
            BdAddress("dma/Data_MM2S", "ps/SAXIGP2/HP0_DDR_LOW", 0, 0x80000000),
            BdAddress("dma/Data_S2MM", "ps/SAXIGP2/HP0_DDR_LOW", 0, 0x80000000),
        ),
        connections=tuple(BdConnection("interface", pins) for pins in (
            ("ps/M_AXI_HPM0_FPD", "control/S00_AXI"),
            ("control/M00_AXI", "poly/s_axi_control"),
            ("control/M01_AXI", "dma/S_AXI_LITE"),
            ("dma/M_AXI_MM2S", "memory/S00_AXI"),
            ("dma/M_AXI_S2MM", "memory/S01_AXI"),
            ("memory/M00_AXI", "ps/S_AXI_HP0_FPD"),
            ("dma/M_AXIS_MM2S", "poly/s_in"),
            ("poly/m_out", "dma/S_AXIS_S2MM"),
        )) + (
            BdConnection("net", ("ps/pl_clk0", "ps/maxihpm0_fpd_aclk", "ps/saxihp0_fpd_aclk",
                "control/aclk", "memory/aclk", "reset/slowest_sync_clk", "poly/ap_clk",
                "dma/s_axi_lite_aclk", "dma/m_axi_mm2s_aclk", "dma/m_axi_s2mm_aclk")),
            BdConnection("net", ("ps/pl_resetn0", "reset/ext_reset_in")),
            BdConnection("net", ("one/dout", "reset/dcm_locked")),
            BdConnection("net", ("reset/interconnect_aresetn", "control/aresetn", "memory/aresetn")),
            BdConnection("net", ("reset/peripheral_aresetn", "poly/ap_rst_n", "dma/axi_resetn")),
        ),
        properties=tuple(BdProperty("cell", cell, key, value) for cell, key, value in (
            ("ps", "VLNV", "xilinx.com:ip:zynq_ultra_ps_e:3.5"),
            ("ps", "CONFIG.PSU__USE__S_AXI_GP2", "1"),
            ("control", "CONFIG.NUM_SI", "1"), ("control", "CONFIG.NUM_MI", "2"),
            ("memory", "CONFIG.NUM_SI", "2"), ("memory", "CONFIG.NUM_MI", "1"),
            ("dma", "VLNV", "xilinx.com:ip:axi_dma:7.1"),
            ("dma", "CONFIG.c_include_sg", "0"),
            ("dma", "CONFIG.c_include_mm2s", "1"),
            ("dma", "CONFIG.c_include_s2mm", "1"),
            ("dma", "CONFIG.c_addr_width", "32"),
            ("dma", "CONFIG.c_sg_length_width", "14"),
            ("dma", "CONFIG.c_m_axis_mm2s_tdata_width", "32"),
            ("dma", "CONFIG.c_s_axis_s2mm_tdata_width", "32"),
            ("dma", "CONFIG.c_include_mm2s_dre", "0"),
            ("dma", "CONFIG.c_include_s2mm_dre", "0"),
            ("reset", "CONFIG.C_EXT_RESET_HIGH", "0"),
        )),
    )
