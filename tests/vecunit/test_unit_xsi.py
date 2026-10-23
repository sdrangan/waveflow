"""The vector unit at RTL (Stage 5 of ``plans/vecunit_new.md``): each unit in its own harness through
the system flow -- codegen, csynth at 4 ns, XSI -- and every output burst and status equal to pysim's,
which equals the interpreter's: the generic unit on random jobs over every instruction form, the CG
unit (four solves) and the matched-filter unit (four frames, long vectors)."""

from __future__ import annotations

import pytest

from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.vecunit.build import build_config, build_dag


def _run(unit, root, workload="test"):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis HLS not found; the XSI gate needs it")
    if not toolchain.find_vivado_path():
        pytest.skip("Vivado (xsim) not found; the XSI gate needs it")
    res = build_dag(unit, workload=workload, n_cycles=400_000).run(build_config(root),
                                                                   through="compare")
    bad = {k: r.message[-3000:] for k, r in res.items() if not r.success}
    assert not bad, bad


@pytest.mark.xsi
def test_xsi_generic_unit(tmp_path):
    """Fusion off (``max_run = max_srun = 1``): with fused runs this configuration's format mix
    drives csynth past 12 GB (Stage 6 record, open); CG and the matched filter run fused."""
    import dataclasses

    from tests.vecunit.test_unit import rtl_config
    from waveflow.vecunit.unit import VecUnit

    cfg = dataclasses.replace(rtl_config(), max_run=1, max_srun=1)
    _run(VecUnit(name="vecunit", sim=Simulation(), cfg=cfg), tmp_path)


@pytest.mark.xsi
def test_xsi_cg_unit(tmp_path):
    from examples.vecunit_cg.cg_unit import CgVecUnit

    _run(CgVecUnit(name="vecunit", sim=Simulation()), tmp_path)


@pytest.mark.xsi
def test_xsi_matched_filter_unit(tmp_path):
    from examples.vecunit_mf.mf_unit import MfVecUnit

    _run(MfVecUnit(name="vecunit", sim=Simulation()), tmp_path)
