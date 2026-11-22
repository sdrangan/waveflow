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


def test_the_fft_probe_builds_the_fft_gates_top(tmp_path):
    """``SsrFft``'s gates already target the RFSoC at 4 ns: the probe's L = 64 builds are theirs, byte for
    byte (``tests/dsp/ssr_fft/test_xsi.py``)."""
    from waveflow.dsp.ssr_fft import rtl as fft_rtl
    for tag, inverse, direction in (("fwd", False, "fixed"), ("inv", True, "fixed"), ("rt", False, "runtime")):
        gate = fft_rtl.generate(tmp_path / f"gate_{tag}", 64, n_frames=8, reorder="pingpong", inverse=inverse,
                                direction=direction, dirs=[0, 1, 1, 0, 1])
        top = rfsoc_probe.generate(tmp_path / f"probe_{tag}", rfsoc_probe.PROBES[f"fft64_{tag}"])
        assert top == gate
        for rel in (f"gen/{top}.tcl", f"gen/{top}.cpp"):
            assert (tmp_path / f"probe_{tag}" / rel).read_text() == (tmp_path / f"gate_{tag}" / rel).read_text()


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


def test_the_stream_blocks_records_are_found_by_the_lookup():
    """The records ``waveflow.streams.calib`` filed and ``waveflow_calib publish`` promoted into the packaged
    ``xczu48dr_250mhz_vitis2025_1``: every swept configuration up to four ports is measured (``EXACT``),
    the policy and the route each on its own key; eight and sixteen were not filed (the attribution's gap,
    ``plans/shared_kernel_rfsoc_timing.md``), so they stay uncalibrated rather than interpolated."""
    from waveflow.calib.platform import Platform, packaged_platforms_dir
    from waveflow.calib.record_store import platform_store
    from waveflow.calib.resource_model import LookupResourceModel
    from waveflow.streams.calib import BLOCKS, PLATFORM

    plat = Platform.resolve(packaged_platforms_dir(), PLATFORM)
    rm = LookupResourceModel(store=platform_store(plat), platform=plat)
    for block in BLOCKS.values():
        keys = set()
        for point in block.grid:
            mod = block.module(point)
            n = point.get("n_in", point.get("n_out"))
            level = rm.confidence(mod).level.value
            assert level == ("EXACT" if n <= 4 else "UNCALIBRATED"), (point, level)
            if n <= 4:
                keys.add(rm.get_params(mod)["module_key"])
                assert rm.predict(mod)["lut"] > 0
        assert len(keys) == 24


def test_pysim_arbiter_spends_one_cycle_between_frames():
    """pysim's ``StreamArbiter`` has no decision cost: one idle cycle between backlogged frames, whatever
    ``n_in`` (the RTL spends 3 to 8 at 100 MHz, ``plans/shared_kernel_rfsoc_timing.md``)."""
    from waveflow.streams.arbiter_timing import pysim_gaps

    for n, policy in ((2, "priority"), (4, "round_robin")):
        g = pysim_gaps(n, policy)
        assert g["frames"] == 12 * n and set(g["gaps"]) == {1}


def test_the_filterbank_probe_builds_the_gates_tops(tmp_path):
    """``examples/os_filterbank``'s tops already target the RFSoC at 4 ns: the probe builds the gates'
    own, byte for byte (``tests/examples/test_os_filterbank.py``)."""
    from examples.os_filterbank import os_filterbank_build as ob

    gate = ob.generate(tmp_path / "gate")
    top = rfsoc_probe.generate(tmp_path / "probe", rfsoc_probe.PROBES["os_filterbank"])
    assert top == gate[0] == "os_filterbank"
    for name in gate:
        for rel in (f"gen/{name}.tcl", f"gen/{name}.cpp"):
            assert (tmp_path / "probe" / rel).read_text() == (tmp_path / "gate" / rel).read_text()
    assert (tmp_path / "probe" / "src" / "os_post_task.h").is_file()


_HIER = """\
| Design State : Routed
+----------------+---------------+------------+---------+-----+--------+------------+
|    Instance    |     Module    | Total LUTs | LUTRAMs | FFs | RAMB36 | DSP Blocks |
+----------------+---------------+------------+---------+-----+--------+------------+
| bd_0_wrapper   |         (top) |        120 |      16 |  90 |      1 |          2 |
|   inst         |     bd_0_top  |        120 |      16 |  90 |      1 |          2 |
|     (inst)     |     bd_0_top  |          4 |       0 |   2 |      0 |          0 |
|     fb_U       |   bd_0_fifo   |         36 |      16 |  28 |      1 |          0 |
|     post_U0    |   bd_0_post   |         80 |       0 |  60 |      0 |          2 |
+----------------+---------------+------------+---------+-----+--------+------------+
"""


def test_the_hierarchical_utilization_parser(tmp_path):
    rpt = tmp_path / "h.rpt"
    rpt.write_text(_HIER)
    rows = rfsoc_probe.hier_util(rpt)
    assert [(r["instance"], r["depth"]) for r in rows] == [
        ("bd_0_wrapper", 0), ("inst", 1), ("(inst)", 2), ("fb_U", 2), ("post_U0", 2)]
    assert (rows[3]["Total LUTs"], rows[3]["LUTRAMs"], rows[3]["FFs"], rows[3]["RAMB36"]) == (36, 16, 28, 1)
    assert rows[4]["DSP Blocks"] == 2 and rows[4]["module"] == "bd_0_post"
