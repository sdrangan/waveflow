"""xbar_profile.py — the crossbar configurations this example measures.

AMD's ``axi_crossbar`` costs area and latency, and both depend on its shape (masters x slaves), its
data width and its **connectivity mode**: ``SAMD`` (a true crossbar -- different master/slave pairs
transfer at once) or ``SASD`` (a shared bus -- one transaction at a time, for the least area).  This
example measures every combination on the RFSoC 4x2 at its 250 MHz fabric clock:

* **area and timing closure** -- out-of-context synthesis, place and route
  (:func:`~waveflow.build.axi_xbar.synth_axi_xbar`);
* **latency in cycles** -- the generated IP under XSI, driven by host and memory BFMs
  (:func:`~waveflow.build.axi_xbar.render_xbar_latency_tb`);

and files both into the platform's module record store under the crossbar's content key
(:meth:`~waveflow.build.axi_xbar.AxiXbarConfig.identity`), where a resource model can read them.

No 1 x 1: the IP refuses it (``AxiXbarConfig``), and a one-master one-slave "crossbar" routes nothing.
1 x 2 (a host reaching two slaves) and 2 x 1 (two masters sharing one memory) are the small shapes
designs actually build.
"""
from __future__ import annotations

from waveflow.build.axi_xbar import AxiXbarConfig, AxiXbarRange

#: The board this example is about: the RFSoC 4x2, at its 250 MHz fabric clock.
RFSOC4X2_PART = "xczu48dr-ffvg1517-2-e"

#: The part the measurements are taken on.  **A stand-in for the RFSoC's ``xczu48dr``**, because
#: synthesizing for an RFSoC needs a Vivado license that covers the device (Enterprise), and the
#: no-cost edition does not.  The ZCU104's ZU7EV is the closest part it does cover: the same Zynq
#: UltraScale+ fabric (CLB, LUT6, routing) and the same -2 speed grade, and ``create_ip`` generates the
#: crossbar's RTL from the *family* (``C_FAMILY = zynquplus``), so the circuit synthesized is the one
#: the RFSoC would get.  With a license that covers it, ``--part xczu48dr-ffvg1517-2-e`` measures
#: the RFSoC itself.
PART = "xczu7ev-ffvc1156-2-e"
CLK_FREQ = 250e6
PERIOD_NS = 1e9 / CLK_FREQ

#: ``"<masters>x<slaves>"``.
SHAPES = ("1x2", "2x1", "2x2", "3x3", "4x4")
MODES = ("SAMD", "SASD")
DATA_WIDTHS = (32, 64)

#: Each slave's window: 64 KB, back to back from address 0.
MI_ADDR_WIDTH = 16

#: Beats in the long-burst probes.
BEATS = 16


def parse_shape(shape: str) -> tuple[int, int]:
    """``"3x3"`` -> ``(3, 3)``."""
    n_si, _x, n_mi = str(shape).partition("x")
    return int(n_si), int(n_mi)


def xbar_config(shape: str = "2x2", mode: str = "SAMD", dw: int = 64,
                part: str = PART) -> AxiXbarConfig:
    """The crossbar for one point.  The ID is just wide enough to name every master."""
    n_si, n_mi = parse_shape(shape)
    return AxiXbarConfig(
        name=f"xbar_{n_si}x{n_mi}_{mode.lower()}_d{int(dw)}", n_si=n_si,
        mi=[AxiXbarRange(k << MI_ADDR_WIDTH, MI_ADDR_WIDTH) for k in range(n_mi)],
        data_width=int(dw), addr_width=32, id_width=max(1, (n_si - 1).bit_length()),
        part=part, mode=mode)
