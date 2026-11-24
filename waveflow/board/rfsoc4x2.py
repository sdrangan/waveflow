"""RFSoC4x2 clock inventory, separate from supported generation recipes.

Sources: RealDigital schematic V3I1 (2024-05-23), sheets 15-18; reference
manual A6, Figure 5 and Appendix A; pinned Vivado preset.xml for PS selectors.
https://www.realdigital.org/downloads/1859152f898cfb0d53c4792e23e91b35.pdf
https://www.realdigital.org/downloads/d6a7543b5033e5dadde272a48bcb5090.pdf

LMK/LMX frequencies are the manual's PYNQ defaults, conditional on runtime
programming. This module neither programs those chips nor chooses HLS targets.
"""

from .spec import ClockDevice, ReferenceClock


def clock_inventory() -> tuple[dict[str, ReferenceClock], dict[str, ClockDevice]]:
    """Return fresh declarative data; do not invoke tools or board software."""
    references = {
        "si5395_crystal": ReferenceClock(48e6, "crystal_48mhz", "XA/XB", ("si5395",)),
        "lmk_vcxo": ReferenceClock(160e6, "vcxo_160mhz", "RF_OUT", ("lmk.OSCIN",)),
        # The preset uses 33.33333 MHz for the schematic's nominal 33 1/3 MHz.
        "ps_reference": ReferenceClock(
            33_333_330, "si5395", "OUT8", ("PS_REF_CLK",),
        ),
        "pl_system_reference": ReferenceClock(
            100e6, "si5395", "OUT0A", ("PL SYS_CLK_100M_P/N",),
        ),
        "pl_ddr_reference": ReferenceClock(
            200e6, "si5395", "OUT1", ("PL_DDR4_REF_CLK_P/N",),
        ),
        "qsfp_reference": ReferenceClock(
            156.25e6, "si5395", "OUT9A", ("PL SYS_CLK_QSFP_P/N",),
        ),
        "qsfp_gt_reference": ReferenceClock(
            156.25e6, "si5395", "OUT9", ("GTY_128_REF_CLK_QSFP_P/N",),
        ),
        "usb3_reference": ReferenceClock(
            100e6, "si5395", "OUT0", ("GTR_505_REF_CLK_USB3_P/N",),
        ),
        "displayport_reference": ReferenceClock(
            27e6, "si5395", "OUT4", ("GTR_505_REF_CLK_DP_P/N",),
        ),
        "usb2_reference": ReferenceClock(
            24e6, "si5395", "OUT6", ("USB2 host PHY", "USB2 slave PHY"),
        ),
        "lmk_reference": ReferenceClock(
            10e6, "si5395", "OUT2", ("lmk.CLKIN0",),
        ),
        "pl_reference": ReferenceClock(
            122.88e6, "lmk", "DCLKout8", ("PL FPGA_REFCLK_IN_P/N",),
        ),
        "pl_sysref": ReferenceClock(
            7.68e6, "lmk", "SDCLKout9", ("PL SYSREF_FPGA_P/N",),
        ),
        "rfdc_sysref": ReferenceClock(
            7.68e6, "lmk", "SDCLKout3", ("RFDC analog SYSREF",),
        ),
        "adc_synth_reference": ReferenceClock(
            245.76e6, "lmk", "DCLKout0", ("lmx_adc.OSCIN",),
        ),
        "dac_synth_reference": ReferenceClock(
            245.76e6, "lmk", "DCLKout4", ("lmx_dac.OSCIN",),
        ),
        "adc_reference": ReferenceClock(
            491.52e6, "lmx_adc", "RFoutA/B", ("RF-ADC tile 226", "RF-ADC tile 224"),
        ),
        "dac_reference": ReferenceClock(
            491.52e6, "lmx_dac", "RFoutA/B", ("RF-DAC tile 230", "RF-DAC tile 228"),
        ),
    }
    devices = {
        "crystal_48mhz": ClockDevice("48 MHz crystal resonator"),
        "vcxo_160mhz": ClockDevice("160 MHz VCXO"),
        "si5395": ClockDevice(
            "Si5395B-A13886-GM", ("si5395_crystal",), "factory_rom",
            notes="Factory configuration; dedicated SI5395 PROG header. Not programmed by the PS preset.",
        ),
        "lmk": ClockDevice(
            "LMK04828", ("lmk_reference", "lmk_vcxo"), "runtime_spi",
            notes="PS SPI setup required. External CLK IN SMA is CLKIN1 in schematic sheet 16; "
                  "manual Figure 5 swaps CLKIN0/1 labels. SYNC IN SMA drives SYNC/SYSREF_REQ.",
        ),
        "lmx_adc": ClockDevice(
            "LMX2594 (PLL1)", ("adc_synth_reference",), "runtime_spi",
            notes="PS SPI setup required; feeds ADC tiles 224 and 226, each with an internal PLL/bypass path.",
        ),
        "lmx_dac": ClockDevice(
            "LMX2594 (PLL2)", ("dac_synth_reference",), "runtime_spi",
            notes="PS SPI setup required; feeds DAC tiles 228 and 230, each with an internal PLL/bypass path.",
        ),
        "ps_apll": ClockDevice(
            "PS APLL", ("ps_reference",), "ps_preset", ("PS ACPU",),
        ),
        "ps_dpll": ClockDevice(
            "PS DPLL", ("ps_reference",), "ps_preset", ("PS DDR controller",),
        ),
        "ps_vpll": ClockDevice("PS VPLL", ("ps_reference",), "ps_preset"),
        "ps_iopll": ClockDevice(
            "PS IOPLL", ("ps_reference",), "ps_preset", ("PS PL0", "PS CPU_R5"),
        ),
        "ps_rpll": ClockDevice("PS RPLL", ("ps_reference",), "ps_preset"),
    }
    return references, devices
