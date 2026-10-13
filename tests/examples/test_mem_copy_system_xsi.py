"""mem_copy through the **system** flow (``plans/system_model.md`` S3c): the kernel as the DUT of an
``HwSystem`` with no crossbar, its testbench -- the command driver, the completion sink and the memory
behind both ``m_axi`` bundles -- answered by the participants' own models.

Two gates, as for any system now: what the testbench recorded (the sink's completions, the memory's
arena) is identical RTL against pysim, and the golden -- every destination equals its source, one
completion per job, the last at **2908** cycles, the single-kernel flow's own gate
(``test_xsi_bfm.py``) -- holds on the RTL's recordings.

The DAG runs with ``synth="check"``: a stale or missing csynth top fails the run, naming it -- it is
never synthesized here, and the gate never grades RTL it did not produce.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.mem_copy.mem_copy_system_build import EXPECTED_CYCLES, HERE, TOP, build_dag, check_xsi
from waveflow.build.build import BuildConfig
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parents[2] / "tests" / "build" / "_xsi_work"


@pytest.fixture(scope="module")
def mem_copy_system_run():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xsim)")
    res = build_dag(work_dir=WORK).run(BuildConfig(root_dir=HERE, params={"synth": "check"}),
                                       through="compare")
    bad = {n: r.message for n, r in res.items() if not r.success}
    if bad:
        pytest.fail(f"the mem_copy system DAG failed: {bad}")
    return WORK / TOP


@pytest.mark.xsi
def test_mem_copy_system_recordings_match_pysim(mem_copy_system_run):
    """The sink's completions and the memory's final arena, RTL against pysim, word for word."""
    work = mem_copy_system_run
    assert json.loads((work / "compare.json").read_text())["trace_mismatches"] == []
    assert sorted(p.name for p in (work / "traces").iterdir()) == ["out", "s_done"]


@pytest.mark.xsi
def test_mem_copy_system_golden_and_cycles(mem_copy_system_run):
    """The golden on the RTL's own recordings, and the last completion at the single-kernel gate's
    cycle: the system flow reproduces the kernel flow exactly."""
    assert check_xsi(mem_copy_system_run) == EXPECTED_CYCLES
