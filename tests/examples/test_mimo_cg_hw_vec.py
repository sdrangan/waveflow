"""Steps 4.2–4.4 of plans/mimo_cg/mimo_cg_paper_sims.md: the CG vector unit ``cg_vec``.

Step 4.2 (no markers): the Python simulation of ``CgVecUnit`` — B and the golden ``S₁ … S_nit``
in from memory, ``cg_vec`` in between, every ``P₀ … P_{nit−1}`` and the final ``X`` out — is
bit-exact against the golden sub-steps, for the frontier formats and the stress set at
K ∈ {4, 8, 16}, with ``nit`` varying across jobs.

Step 4.3 (``-m vitis``): the hand-written C++ (``hw/cpp/``) is bit-exact in Vitis C-simulation on
the same scenarios, and ``CgVecUnit`` synthesizes on ``xczu48dr-ffvg1517-2-e`` with an estimated
clock of at most 4 ns at K ∈ {4, 8, 16}.

Step 4.4 (``-m xsi``): the synthesized RTL, driven cycle by cycle through the generated XSI
harness, is bit-exact on the same kind of scenario at K = 4.  It needs the step 4.3 csynth of
``build/cg_vec_unit_k4_f0`` and skips loudly when that RTL is missing or stale.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw.build import (
    PART,
    VEC_TOP,
    build_dir,
    check_xsi_outputs,
    csynth,
    generate_tb,
    generate_vec_unit,
    run_xsi,
)
from examples.mimo_cg.hw.common import HW_FORMAT_NAMES, hw_format
from examples.mimo_cg.hw.csim import VEC_REPS, run_csim
from examples.mimo_cg.hw.vec import CgVecUnit, CgVecUnitSim, block_type
from examples.mimo_cg.mimo_cg_conformance import CaseSetSpec, _problems
from waveflow.build.trace_steps import rtl_staleness
from waveflow.hw.mem_stream import MemRStream, MemWStream
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.csynthparse import CsynthParser, synth_target

N = 32


def _jobs(fmt_id: int, K: int, seed: int):
    """51 problems (50 random + the zero-residual one) and a nit per job cycling 1..K."""
    probs = _problems(CaseSetSpec("vec", hw_format(fmt_id), K, N, K, False, seed))
    return [(A, B, 64.0) for A, B, _ in probs], [(j % K) + 1 for j in range(len(probs))]


@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt_id", range(len(HW_FORMAT_NAMES)), ids=HW_FORMAT_NAMES)
def test_vec_unit_pysim_is_bit_exact(fmt_id, K):
    problems, jobs = _jobs(fmt_id, K, seed=500 + 10 * fmt_id + K)
    assert len(problems) >= 51
    CgVecUnitSim(problems, jobs, K=K, fmt=fmt_id).run()  # raises on any mismatch


def test_vec_unit_pysim_with_32_bit_memory_words():
    problems, jobs = _jobs(1, 8, seed=590)
    CgVecUnitSim(problems[:10], jobs[:10], K=8, fmt=1, mem_dwidth=32).run()


def test_vec_unit_is_built_on_the_framework_mem_streams():
    unit = CgVecUnit(name="u", sim=Simulation(), K=4, fmt=0)
    assert isinstance(unit.rstream, MemRStream) and unit.rstream.inband
    assert isinstance(unit.wstream, MemWStream) and unit.wstream.inband
    assert unit.wstream.emit_done
    assert unit.m_in is unit.rstream.m_mem and unit.m_out is unit.wstream.m_mem
    names = [b[0] if isinstance(b, tuple) else b for b in unit.boundary]
    assert names == ["s_cmd", "m_in", "m_out", "s_done"]


def test_blocks_are_lane_groups():
    blk = block_type(12, 16, 32, 4)
    assert blk.max_shape == (16 * 32 // 4,)
    assert blk.element_type.get_bitwidth() == 2 * 12 * 4
    with pytest.raises(ValueError, match="multiple of the lane count"):
        block_type(12, 4, 30, 4)


# --- step 4.3: C-simulation and csynth (Vitis) ----------------------------------------------


def _require_vitis() -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")


@pytest.mark.vitis
@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt_id", range(len(HW_FORMAT_NAMES)), ids=HW_FORMAT_NAMES)
def test_vec_unit_csim_is_bit_exact(tmp_path, fmt_id, K):
    """The hand-written C++ (and the framework mem-stream bodies) in sequential Vitis C-sim: 12 jobs
    (the last is the zero-residual case), every P and X word exact and one done per job.
    """
    _require_vitis()
    generate_vec_unit(tmp_path, K=K, fmt=fmt_id)
    problems, jobs = _jobs(fmt_id, K, seed=700 + 10 * fmt_id + K)
    sim = CgVecUnitSim(problems[-12:], jobs[-12:], K=K, fmt=fmt_id)
    result = run_csim(tmp_path, VEC_TOP, 64, sim.scenario(), jobs[-12:], VEC_REPS)
    assert result["ok"], result["log"][-3000:]


@pytest.mark.vitis
@pytest.mark.parametrize("K", [4, 8, 16])
def test_vec_unit_csynth_meets_4_ns(K):
    """csynth of CgVecUnit (W12g8, L = 4) into its build directory, which the RTL gate reuses."""
    _require_vitis()
    d = build_dir(VEC_TOP, K, 0)
    generate_vec_unit(d, K=K, fmt=0)
    ok, report, log = csynth(d, VEC_TOP)
    assert ok, log[-3000:]
    target = synth_target(report)
    assert target is not None
    assert (
        target["part"].startswith(PART.split("-")[0])
        and target["target_period_ns"] == 4.0
    )
    assert target["estimated_period_ns"] <= 4.0, target
    parser = CsynthParser(report_path=str(report))
    parser.get_total_resources()
    assert parser.total_resources, "no resource totals in the report"


# --- step 4.4: the RTL through XSI -----------------------------------------------------------


@pytest.mark.xsi
def test_vec_unit_rtl_is_bit_exact():
    """12 jobs (nit cycling 1..4, the last the zero-residual case) through the K = 4 RTL: every
    P and X word exact, one done per job, no deadlock (the run completes within its bound).
    """
    d = build_dir(VEC_TOP, 4, 0)
    if not (d / f"{VEC_TOP}_proj").is_dir():
        pytest.skip(f"no csynth RTL at {d} -- run the step 4.3 csynth test first")
    why = rtl_staleness(d, VEC_TOP)
    if why is not None:
        pytest.skip(f"XSI gate prerequisite missing: {why}")
    problems, jobs = _jobs(0, 4, seed=800)
    sim = CgVecUnitSim(problems[-12:], jobs[-12:], K=4, fmt=0)
    scenario = generate_tb(d, VEC_TOP, sim.tb, sim)
    proc = run_xsi(d, VEC_TOP)
    assert (
        proc.returncode == 0
    ), f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    assert (
        d / "xsi" / "vectors" / "out"
    ).exists(), "the XSI run produced no memory dump"
    check_xsi_outputs(d, scenario)
