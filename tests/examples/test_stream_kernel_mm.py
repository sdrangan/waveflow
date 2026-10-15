"""The free-running poly behind AXI-MM with a two-thread host (``examples/stream_kernel/stream_kernel_mm.py``,
``plans/unit_harness.md`` Stage 2): the same kernel as the stream harness, its queue views on a crossbar.

pysim against the golden; the host never polls; the system is built the D12 way.  At RTL: the host's
traces equal pysim's, and the **composition check** (D8) -- pysim's cycles, composed from the kernel's
calibrated phases and the bus and queue models with nothing fitted to this system, within 5% of the RTL.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from examples.stream_kernel.stream_kernel import default_cmds
from examples.stream_kernel.stream_kernel_build import HERE, MM_TOP, build_dag
from examples.stream_kernel.stream_kernel_mm import HOST_ENDPOINTS, mm_system
from tests.examples._system_run import WORK, system_run

#: The RTL's cycle count for the default commands (Vitis/Vivado 2025.1, XSI): the composition check's
#: reference.  pysim measured 396 when this was recorded.
RTL_CYCLES = 392


def _run():
    sysm = mm_system()
    return sysm, sysm.run()


def test_pysim_matches_the_golden_through_the_bus():
    """Every result burst and response, bit for bit -- including the command whose samples end early,
    whose results are as many as the host sent."""
    _sysm, out = _run()
    assert len(out["results"]) == len(out["want_results"]) == len(default_cmds())
    for got, want in zip(out["results"], out["want_results"]):
        np.testing.assert_array_equal(np.asarray(got, np.uint64), np.asarray(want, np.uint64))
    for got, want in zip(out["resps"], out["want_resps"]):
        np.testing.assert_array_equal(np.asarray(got, np.uint64), np.asarray(want, np.uint64))


def test_the_host_never_polls():
    """Every endpoint waits on its queue's interrupt: no read of a queue's status half."""
    sysm = mm_system()
    host, reads = sysm.host, []
    orig = host.m.read

    def spy(nwords, addr, *a, **k):
        reads.append(int(addr))
        return orig(nwords, addr, *a, **k)

    host.m.read = spy
    sysm.run()
    status = {sysm.device.layout.at(0)[n].status_addr for n in ("q_out", "q_resp")}
    assert not (set(reads) & status)


def test_it_is_built_the_d12_way():
    """``HwSystem(sim, dut)``: the DUT is the device and the crossbar; the host is the testbench."""
    sysm = mm_system()
    assert [p.name for p in sysm.participants()] == ["host"]
    assert [type(c).__name__ for c in sysm.dut().sub_comps.values()] == ["MmSlaveDevice"]
    assert [m.name for m in sysm.cut()] == ["poly_kernel"]


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    return system_run(build_dag(work_dir=WORK), HERE, f"mm_{MM_TOP}", through="mm_compare")


@pytest.mark.xsi
def test_rtl_host_traces_match_pysim(rtl_run):
    """THE gate: what the host's three endpoints carried at RTL is what they carried in pysim, word
    for word (``mm_compare``), and the C++ host finished."""
    assert sorted(p.name for p in (rtl_run / "traces").iterdir()) == sorted(HOST_ENDPOINTS)
    assert json.loads((rtl_run / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
    assert json.loads((rtl_run / "report.json").read_text(encoding="utf-8"))["done"] is True


@pytest.mark.xsi
def test_composition_holds_without_fitting(rtl_run):
    """D8: the device's timing composed from its parts' models -- the kernel's calibrated phases (Stage
    1), the queue views' and the crossbar's -- with nothing fitted here, within 5% of its RTL.  Before
    queue in cut through (its packets begun as their first word lands), pysim lost the kernel's
    overlap of samples and results and was 19% slow (466 against 392)."""
    rtl = json.loads((rtl_run / "report.json").read_text(encoding="utf-8"))["cycles"]
    py = json.loads((rtl_run / "pysim.json").read_text(encoding="utf-8"))["cycles"]
    assert rtl == RTL_CYCLES
    assert abs(py - rtl) <= 0.05 * rtl, f"pysim {py} against RTL {rtl}"


# ---------------------------------------------------------------------------
# The device's own harness (D9): the host on a direct link to the device's one front, no crossbar
# ---------------------------------------------------------------------------

#: The device harness's RTL cycles for the default commands (Vitis/Vivado 2025.1, XSI).  pysim
#: measured 421 when this was recorded (the direct link at zero latency: it is wires).
DEV_RTL_CYCLES = 408


def test_the_device_harness_runs_without_a_crossbar():
    """Bit-exact in pysim, and its link realizes as wires: no crossbar IP, a direct-link config."""
    from waveflow.build.system_top import SystemTopSpec, render_system_top

    sysm = mm_system(direct=True)
    out = sysm.run()
    for got, want in zip(out["results"] + out["resps"], out["want_results"] + out["want_resps"]):
        np.testing.assert_array_equal(np.asarray(got, np.uint64), np.asarray(want, np.uint64))
    spec = SystemTopSpec.from_system(sysm, top="dev_top")
    assert spec.xbar.direct and len(spec.mi) == 1 and spec.mi[0].kind == "adaptor"
    text = render_system_top(spec)
    assert "axi_crossbar" not in text and "direct link: s0_axi -> mi0_axi" in text


@pytest.fixture(scope="module")
def dev_run() -> Path:
    from examples.stream_kernel.stream_kernel_build import DEV_TOP

    return system_run(build_dag(work_dir=WORK), HERE, f"dev_{DEV_TOP}", through="dev_compare")


@pytest.mark.xsi
def test_rtl_device_harness_traces_match_pysim(dev_run):
    """The device alone at RTL, its host on a direct link: the host's traces equal pysim's."""
    assert json.loads((dev_run / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
    assert json.loads((dev_run / "report.json").read_text(encoding="utf-8"))["done"] is True


@pytest.mark.xsi
def test_device_composition_holds_without_fitting(dev_run):
    """D8 on the device's own harness: its timing composed from its parts' models (the kernel's
    calibrated phases, the views and the one front), nothing fitted, within 5% of its RTL."""
    rtl = json.loads((dev_run / "report.json").read_text(encoding="utf-8"))["cycles"]
    py = json.loads((dev_run / "pysim.json").read_text(encoding="utf-8"))["cycles"]
    assert rtl == DEV_RTL_CYCLES
    assert abs(py - rtl) <= 0.05 * rtl, f"pysim {py} against RTL {rtl}"
