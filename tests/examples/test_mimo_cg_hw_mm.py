"""Steps 4.5–4.7 of plans/mimo_cg/mimo_cg_paper_sims.md: the systolic matrix multiply ``cg_mm``.

Step 4.5 (no markers): the Python simulation of ``CgMmUnit`` — A and the golden
``P₀ … P_{nit−1}`` in from memory, ``cg_mm`` in between, every ``S₁ … S_nit`` out — is bit-exact
against the golden ``mm_step``, for the frontier formats and the stress set at K ∈ {4, 8, 16},
both complex-multiply forms.

Step 4.6 (``-m vitis``): the hand-written C++ is bit-exact in sequential Vitis C-sim on the same
scenarios, and ``CgMmUnit`` synthesizes on ``xczu48dr-ffvg1517-2-e`` with an estimated clock of at
most 4 ns at K ∈ {4, 8, 16} (``cmul = 4``) and at K = 8 with ``cmul = 3``, which uses fewer DSPs.

Step 4.7 (``-m xsi``): the K = 4 RTL of both multiply forms, driven through the generated XSI
harness, is bit-exact; it needs the step 4.6 csynth and skips loudly when that RTL is missing or
stale.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw.build import (
    MM_TOP,
    PART,
    build_dir,
    check_xsi_outputs,
    csynth,
    generate_mm_unit,
    generate_tb,
    run_xsi,
)
from examples.mimo_cg.hw.common import HW_FORMAT_NAMES, hw_format
from examples.mimo_cg.hw.csim import MM_REPS, run_csim
from examples.mimo_cg.hw.mm import CgMm, CgMmUnit, CgMmUnitSim, a_block_type
from examples.mimo_cg.mimo_cg_conformance import CaseSetSpec, _problems
from waveflow.build.trace_steps import rtl_staleness
from waveflow.hw.mem_stream import MemRStream, MemWStream
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.csynthparse import CsynthParser, synth_target

N = 32


def _jobs(fmt_id: int, K: int, seed: int):
    """51 problems (50 random + the zero-residual one) and a nit per job cycling 1..K."""
    probs = _problems(CaseSetSpec("mm", hw_format(fmt_id), K, N, K, False, seed))
    return [(A, B, 64.0) for A, B, _ in probs], [(j % K) + 1 for j in range(len(probs))]


# --- step 4.5: the Python simulation ---------------------------------------------------------


@pytest.mark.parametrize("cmul", [4, 3])
@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt_id", range(len(HW_FORMAT_NAMES)), ids=HW_FORMAT_NAMES)
def test_mm_unit_pysim_is_bit_exact(fmt_id, K, cmul):
    problems, jobs = _jobs(fmt_id, K, seed=600 + 10 * fmt_id + K)
    CgMmUnitSim(problems, jobs, K=K, fmt=fmt_id, cmul=cmul).run()


def test_mm_unit_is_built_on_the_framework_mem_streams():
    unit = CgMmUnit(name="u", sim=Simulation(), K=4, fmt=0)
    assert isinstance(unit.rstream, MemRStream) and unit.rstream.inband
    assert isinstance(unit.wstream, MemWStream) and unit.wstream.emit_done
    names = [b[0] if isinstance(b, tuple) else b for b in unit.boundary]
    assert names == ["s_cmd", "m_in", "m_out", "s_done"]


def test_array_shape_is_checked():
    sim = Simulation()
    assert CgMm(name="m", sim=sim, K=8, R=0).rows == 8  # R = 0 means R = K
    with pytest.raises(ValueError, match="R | K"):
        CgMm(name="m2", sim=sim, K=8, R=3)
    with pytest.raises(ValueError, match="cmul"):
        CgMm(name="m3", sim=sim, K=4, cmul=2)
    assert a_block_type(12, 16).element_type.get_bitwidth() == 2 * 12 * 16


# --- step 4.6: C-simulation and csynth (Vitis) ----------------------------------------------


def _require_vitis() -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")


@pytest.mark.vitis
@pytest.mark.parametrize("cmul", [4, 3])
@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt_id", range(len(HW_FORMAT_NAMES)), ids=HW_FORMAT_NAMES)
def test_mm_unit_csim_is_bit_exact(tmp_path, fmt_id, K, cmul):
    """Sequential C-sim of the generated unit: 12 jobs (the last is the zero-residual case), every
    S word exact and one done per job."""
    _require_vitis()
    generate_mm_unit(tmp_path, K=K, fmt=fmt_id, cmul=cmul)
    problems, jobs = _jobs(fmt_id, K, seed=900 + 10 * fmt_id + K)
    sim = CgMmUnitSim(problems[-12:], jobs[-12:], K=K, fmt=fmt_id, cmul=cmul)
    result = run_csim(tmp_path, MM_TOP, 64, sim.scenario(), jobs[-12:], MM_REPS)
    assert result["ok"], result["log"][-3000:]


def _synth(K: int, cmul: int) -> dict:
    d = build_dir(MM_TOP, K, 0, cmul=cmul)
    generate_mm_unit(d, K=K, fmt=0, cmul=cmul)
    ok, report, log = csynth(d, MM_TOP)
    assert ok, log[-3000:]
    target = synth_target(report)
    assert target is not None
    assert target["part"].startswith(PART.split("-")[0])
    assert target["target_period_ns"] == 4.0
    assert target["estimated_period_ns"] <= 4.0, target
    parser = CsynthParser(report_path=str(report))
    parser.get_total_resources()
    return parser.total_resources


@pytest.mark.vitis
@pytest.mark.parametrize("K", [4, 8, 16])
def test_mm_unit_csynth_meets_4_ns(K):
    """csynth of CgMmUnit (W12g8, R = K, C = 4, cmul = 4) into its build directory."""
    _require_vitis()
    assert _synth(K, 4)["DSP"] > 0


@pytest.mark.vitis
def test_three_multiply_form_meets_4_ns_with_fewer_dsps():
    """K = 8: the Gauss form synthesizes at 4 ns and uses fewer DSPs than the 4-multiply form."""
    _require_vitis()
    four, three = _synth(8, 4), _synth(8, 3)
    assert three["DSP"] < four["DSP"], (three, four)


# --- step 4.7: the RTL through XSI -----------------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("cmul", [4, 3])
def test_mm_unit_rtl_is_bit_exact(cmul):
    """12 jobs (nit cycling 1..4, the last the zero-residual case) through the K = 4 RTL: every
    S word exact, one done per job, no deadlock (the run completes within its bound)."""
    d = build_dir(MM_TOP, 4, 0, cmul=cmul)
    if not (d / f"{MM_TOP}_proj").is_dir():
        pytest.skip(f"no csynth RTL at {d} -- run the step 4.6 csynth first")
    why = rtl_staleness(d, MM_TOP)
    if why is not None:
        pytest.skip(f"XSI gate prerequisite missing: {why}")
    problems, jobs = _jobs(0, 4, seed=1000 + cmul)
    sim = CgMmUnitSim(problems[-12:], jobs[-12:], K=4, fmt=0, cmul=cmul)
    scenario = generate_tb(d, MM_TOP, sim.tb, sim)
    proc = run_xsi(d, MM_TOP)
    assert (
        proc.returncode == 0
    ), f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    assert (
        d / "xsi" / "vectors" / "out"
    ).exists(), "the XSI run produced no memory dump"
    check_xsi_outputs(d, scenario)
