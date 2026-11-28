"""``waveflow.build.rtl_ooc``'s register ring (``plans/bram_fullrate_port.md``, Stage 3) without Vivado:
the wrapper registers every port but the clock once and keeps the DUT's hierarchy, and the TCL reports
the path groups the timing-closure gate reads.  The runs themselves are
``tests/build/test_mm_bram_timing_vitis.py`` (``-m vitis``)."""
from __future__ import annotations

from waveflow.build.rtl_ooc import RING_DUT, RING_PATHS, registered_wrapper, ring_tcl

DUT = """
module toy #(parameter integer W = 8) (
    input  wire          ap_clk,
    input  wire          ap_rst_n,
    input  wire [7:0]    a,
    input  wire          v,
    output wire [7:0]    y,
    output wire          r
);
endmodule
"""


def test_every_port_but_the_clock_goes_through_one_flop(tmp_path):
    src = tmp_path / "toy.v"
    src.write_text(DUT, encoding="utf-8")
    w = registered_wrapper("toy", src)
    assert "module toy_ring (" in w and "input wire ap_clk," in w
    for name, rng in (("ap_rst_n", ""), ("a", "[7:0] "), ("v", "")):
        assert f"reg  {rng}i_{name};" in w and f"i_{name} <= {name};" in w and f".{name}(i_{name})" in w
    for name, rng in (("y", "[7:0] "), ("r", "")):
        assert f"reg  {rng}o_{name};" in w and f"o_{name} <= d_{name};" in w
        assert f"assign {name} = o_{name};" in w and f".{name}(d_{name})" in w
    assert "i_ap_clk" not in w and ".ap_clk(ap_clk)" in w
    assert f'(* keep_hierarchy = "yes" *) toy {RING_DUT} (' in w


def test_the_tcl_routes_and_reports_each_path_group():
    t = ring_tcl("toy_ring", ["a.v", "b.v"], part="xczu48dr-ffvg1517-2-e", paths=RING_PATHS)
    assert "synth_design -top toy_ring -part xczu48dr-ffvg1517-2-e -mode out_of_context" in t
    for step in ("opt_design", "place_design", "phys_opt_design", "route_design"):
        assert step in t
    assert t.count("report_utilization -hierarchical") == 2
    for name in RING_PATHS:
        assert f"WF_SLACKOF_{name}" in t
    assert t.index("route_design") < t.index("wf_slack impl") < t.index("WF_SLACKOF_from_bram")
    assert "DOA_REG" in t and "DOB_REG" in t and 'puts "WF_BRAMREGS' in t
