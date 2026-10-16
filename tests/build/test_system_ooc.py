"""``waveflow/build/system_ooc.py`` -- a system synthesized per device and linked
(``plans/ip_packaging_routes.md`` Stage 1), the toolchain-free half: the black-box top, the link
script, the link term's arithmetic, and the out-of-context run keeping its netlist.  The Vivado run
itself is measured by hand (the plan's Stage 1 results) -- it is minutes, not a unit test."""
from __future__ import annotations

import re

from waveflow.build.system_ooc import OocLink, black_box, link_tcl, top_with_black_boxes


def _spec():
    from examples.markov.markov_build import system
    from waveflow.build.system_top import SystemTopSpec

    return SystemTopSpec.from_system(system(), top="markov_top")


def test_a_black_box_keeps_the_ports_and_drops_the_body():
    text = "module dev (\n  input wire ap_clk,\n  output wire irq_x\n);\n  wire a;\n  assign irq_x = a;\nendmodule\n"
    stub = black_box(text)
    assert stub.startswith("(* black_box *)\nmodule dev (")
    assert "input wire ap_clk" in stub and "output wire irq_x" in stub
    assert "assign" not in stub and stub.rstrip().endswith("endmodule")


def test_the_top_is_synthesized_against_a_black_box_per_sub_top():
    spec = _spec()
    v = top_with_black_boxes(spec)
    assert v.count("(* black_box *)") == len(spec.subs) == 3
    assert re.search(r"^module markov_top \(", v, re.M)
    for sub in spec.subs:
        assert f"  {sub.module} {sub.inst} (" in v                      # instantiated by the top
        assert f"module {sub.module} (" in v                           # ...and declared as a stub
    assert "u_gen_qcmd" not in v                                      # no sub-top's body


def test_the_top_is_synthesized_once_and_kept():
    from waveflow.build.system_ooc import top_tcl

    lines = top_tcl("markov_top", part="p", xci="x.xci", top_v="markov_top.v").splitlines()
    synth = lines.index("synth_design -top markov_top -part p -mode out_of_context")
    assert lines.index("read_ip {x.xci}") < synth < lines.index("write_checkpoint -force top.dcp")


def test_the_link_scopes_each_netlist_to_its_cell_then_optimizes():
    lines = link_tcl("markov_top", part="p", top_dcp="t.dcp",
                     cells={"u_gen_sub": "g.dcp", "u_mem_sub": "m.dcp"}).splitlines()
    assert "synth_design" not in "\n".join(lines)                       # nothing re-synthesized
    scoped = [i for i, line in enumerate(lines) if "SCOPED_TO_CELLS" in line]
    assert [lines[i] for i in scoped] == [
        "set_property SCOPED_TO_CELLS {u_gen_sub} [get_files {g.dcp}]",
        "set_property SCOPED_TO_CELLS {u_mem_sub} [get_files {m.dcp}]"]
    link = lines.index("link_design -top markov_top -part p -mode out_of_context")
    assert lines.index("add_files {t.dcp}") < scoped[0] < link < lines.index("opt_design")


def test_the_link_term_is_what_linking_changed():
    r = OocLink(top="t", part="p", period_ns=10.0,
                parts={"a": {"lut": 100, "ff": 50}, "b": {"lut": 10, "ff": 5}},
                top_counters={"lut": 20, "ff": 8}, linked={"lut": 120, "ff": 63})
    assert r.link_term == {"ff": 0, "lut": -10}
    assert r.to_json()["link_term"] == r.link_term


def test_an_ooc_run_can_keep_its_netlist():
    from waveflow.build.rtl_ooc import ooc_tcl

    assert "write_checkpoint" not in ooc_tcl("m", {}, ["a.v"], part="p")
    assert "write_checkpoint -force design.dcp" in ooc_tcl("m", {}, ["a.v"], part="p", checkpoint=True)
