"""The RFSoC timing probe (``waveflow/streams/rfsoc_probe.py``, ``plans/shared_kernel_rfsoc_timing.md``):
its tops target the RFSoC part at 4 ns while the gates' stay on the default part, and it reads a schedule
report's critical paths.  No tool needed."""
from __future__ import annotations

from waveflow.build.composite_gen import DEFAULT_PART, RFSOC4X2_PART, tcl_path
from waveflow.streams import rfsoc_probe, rtl


def _target(tcl: str) -> tuple[str, str]:
    lines = tcl.splitlines()
    part = next(ln for ln in lines if ln.startswith("set part"))
    clock = next(ln for ln in lines if ln.startswith("create_clock"))
    return part, clock


def test_the_probe_targets_the_rfsoc_and_the_gates_do_not(tmp_path):
    top = rfsoc_probe.generate(tmp_path / "probe", rfsoc_probe.PROBES["pair_p_f2"])
    assert _target(tcl_path(tmp_path / "probe", top).read_text()) == (
        f"set part {{{RFSOC4X2_PART}}}", "create_clock -period 4")
    mod, _ = rtl.make("pair", 3, 32, 2, "priority")
    gate = rtl.generate_for(tmp_path / "gate", mod)
    assert _target(tcl_path(tmp_path / "gate", gate).read_text()) == (
        f"set part {{{DEFAULT_PART}}}", "create_clock -period 10")


def test_the_ring_stub_probe_carries_its_bodies(tmp_path):
    top = rfsoc_probe.generate(tmp_path, rfsoc_probe.PROBES["ring_stub"])
    assert top == "ring_stub"
    assert (tmp_path / "src" / "ring_mux_task.h").is_file()
    assert _target(tcl_path(tmp_path, top).read_text())[0] == f"set part {{{RFSOC4X2_PART}}}"


_RPT = """\
============================================================
+ Verbose Summary: Timing violations
============================================================
Target clock period: 4.000ns, clock uncertainty: 1.080ns.

 <State 1>: 0.427ns
The critical path consists of the following:
\t'alloca' operation 16 bit ('empty') [4]  (0.000 ns)
\t'store' operation 0 bit ('store_ln0') of constant 0 on local variable 'empty' [9]  (0.427 ns)

 <State 2>: 2.920ns
The critical path consists of the following:
\tfifo read operation ('fb_read') on port 'fb' [22]  (1.460 ns)
\tfifo write operation ('write_ln47') on port 'k_in' [26]  (1.460 ns)

============================================================
+ Verbose Summary: Binding
============================================================
"""


def test_the_schedule_report_parser(tmp_path):
    rpt = tmp_path / "m.verbose.sched.rpt"
    rpt.write_text(_RPT)
    sp = rfsoc_probe.sched_paths(rpt)
    assert (sp["target_ns"], sp["uncertainty_ns"]) == (4.0, 1.08)
    assert [(s["state"], s["delay_ns"], len(s["ops"])) for s in sp["states"]] == [(1, 0.427, 2), (2, 2.92, 2)]
    assert sp["states"][1]["ops"][0].startswith("fifo read operation ('fb_read')")
