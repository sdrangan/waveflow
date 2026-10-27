"""The vector unit at RTL (Stage 5 of ``plans/vecunit_new.md``): each unit in its own harness through
the system flow -- codegen, csynth at 4 ns, XSI -- and every output burst and status equal to pysim's,
which equals the interpreter's: the generic unit on random jobs over every instruction form, the CG
unit (four solves) and the matched-filter unit (four frames, long vectors); the last two also at
128-bit data words (Stage 9a: a lane group of four 32-bit complex values is then one whole word)."""

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


@pytest.mark.xsi
def test_xsi_cg_unit_128(tmp_path):
    from examples.vecunit_cg.cg_unit import CgVecUnit

    _run(CgVecUnit(name="vecunit", sim=Simulation(), word_bits=128), tmp_path)


@pytest.mark.xsi
def test_xsi_matched_filter_unit_128(tmp_path):
    from examples.vecunit_mf.mf_unit import MfVecUnit

    _run(MfVecUnit(name="vecunit", sim=Simulation(), word_bits=128), tmp_path)


@pytest.mark.xsi
def test_xsi_toy_unit(tmp_path):
    """The toy kernels (``examples/vecunit``), the scalar extensions and the result shift among them
    (``plans/vecunit_future.md`` F2, F4): a build with ``rs_max`` 15."""
    from examples.vecunit.harness import unit

    _run(unit(), tmp_path)


@pytest.mark.xsi
def test_xsi_toy_unit_fused_dependent_passes(tmp_path):
    """F7 at RTL: ``poly``'s two dependent passes fused into one run at ``VL`` 64 (``fuse_raw_vl``
    64), bit-exact; and the build's fused loop is shallower than 64, which is what makes it safe."""
    from examples.vecunit.harness import unit
    from waveflow.vecunit import report

    _run(unit(VLMAX=64, fuse_raw_vl=64), tmp_path, workload="poly_64")
    _ii, depth = report.loop_schedule(tmp_path)["run"]
    assert depth < 64, f"the fused loop is {depth} deep: fuse_raw_vl 64 would not be safe"
