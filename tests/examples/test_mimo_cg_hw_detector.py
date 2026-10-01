"""Steps 4.8–4.10 of plans/mimo_cg/mimo_cg_paper_sims.md: the integrated CG detector (AC4).

Step 4.8 (no markers): the Python simulation of ``CgDetector`` — CG control, the vector unit and
the systolic matmul in a feedback loop over stream-of-blocks — writes, for every job, the ``X`` of
``cg_fixed`` itself, bit for bit: every problem at every ``nit = 1 … K``, K ∈ {4, 8, 16}, W12g8 and
W14g8, with many jobs in flight.

Step 4.9 (``-m vitis``): ``CgDetector`` synthesizes on ``xczu48dr-ffvg1517-2-e`` with an estimated
clock of at most 4 ns at K ∈ {4, 8, 16} (W12g8) and at K = 4 with W14g8.

Step 4.10 (``-m xsi``): the K = 4 RTL, on problems from M = 32 channels with jobs nit = 1 … 4, is
bit-exact for W12g8 and W14g8 (AC4's integrated check); it needs the step 4.9 csynth and skips
loudly when that RTL is missing or stale.
"""

from __future__ import annotations

import pytest

from examples.mimo_cg.hw.build import (
    DET_TOP,
    PART,
    build_dir,
    check_xsi_outputs,
    csynth,
    generate_detector,
    generate_tb,
    run_xsi,
)
from examples.mimo_cg.hw.detector import CgDetector, CgDetectorSim, detector_problems
from waveflow.build.trace_steps import rtl_staleness
from waveflow.hw.mem_stream import MemRStream, MemWStream
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain import toolchain
from waveflow.utils.csynthparse import CsynthParser, synth_target

N = 32
FRONTIER = {"W12g8": 0, "W14g8": 1}


def _every_nit(M: int, K: int, n: int, seed: int):
    """``n`` problems (the last the zero-residual case), each run as a job at every nit 1..K."""
    probs = detector_problems(M, K, N, n, seed)
    return [p for p in probs for _ in range(K)], [
        nit for _ in probs for nit in range(1, K + 1)
    ]


# --- step 4.8: the Python simulation ---------------------------------------------------------


@pytest.mark.parametrize("K", [4, 8, 16])
@pytest.mark.parametrize("fmt", list(FRONTIER))
def test_detector_pysim_matches_cg_fixed_at_every_nit(fmt, K):
    problems, jobs = _every_nit(
        32 if K == 4 else 64, K, 20, seed=K + 10 * FRONTIER[fmt]
    )
    CgDetectorSim(problems, jobs, K=K, fmt=FRONTIER[fmt]).run()


def test_detector_with_other_knobs():
    """The other knobs keep the detector bit-exact: the Gauss matmul, 8 lanes with an 8-wide array."""
    problems, jobs = _every_nit(32, 8, 6, seed=7)
    CgDetectorSim(problems, jobs, K=8, fmt=0, cmul=3).run()
    CgDetectorSim(problems, jobs, K=8, fmt=0, L=8, C=8).run()


def test_detector_structure():
    det = CgDetector(name="d", sim=Simulation(), K=4, fmt=0)
    assert isinstance(det.rstream, MemRStream) and det.rstream.inband
    assert isinstance(det.wstream, MemWStream) and det.wstream.emit_done
    names = [b[0] if isinstance(b, tuple) else b for b in det.boundary]
    assert names == ["s_cmd", "m_in", "m_out", "s_done"]
    # the feedback loop: vec -> mm through p_blk, mm -> vec through s_blk
    assert det.vec.p_blk.element_type is det.mm.p_blk.element_type
    assert det.mm.s_blk.element_type is det.vec.s_blk.element_type


# --- step 4.9: csynth (Vitis) ----------------------------------------------------------------


def _require_vitis() -> None:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")


@pytest.mark.vitis
@pytest.mark.parametrize(("K", "fmt"), [(4, 0), (8, 0), (16, 0), (4, 1)])
def test_detector_csynth_meets_4_ns(K, fmt):
    """csynth of CgDetector (default knobs) into its build directory, which the RTL gate reuses."""
    _require_vitis()
    d = build_dir(DET_TOP, K, fmt)
    generate_detector(d, K=K, fmt=fmt)
    ok, report, log = csynth(d, DET_TOP)
    assert ok, log[-3000:]
    target = synth_target(report)
    assert target is not None
    assert target["part"].startswith(PART.split("-")[0])
    assert target["target_period_ns"] == 4.0
    assert target["estimated_period_ns"] <= 4.0, target
    parser = CsynthParser(report_path=str(report))
    parser.get_total_resources()
    assert parser.total_resources["DSP"] > 0


# --- step 4.10: the RTL through XSI (AC4) ----------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("fmt", list(FRONTIER))
def test_detector_rtl_is_bit_exact_at_m32_k4(fmt):
    """Problems from M = 32, K = 4 channels (the last the zero-residual case), each at nit = 1..4,
    through the K = 4 RTL: every X word equals cg_fixed's, one done per job, no deadlock.
    """
    d = build_dir(DET_TOP, 4, FRONTIER[fmt])
    if not (d / f"{DET_TOP}_proj").is_dir():
        pytest.skip(f"no csynth RTL at {d} -- run the step 4.9 csynth first")
    why = rtl_staleness(d, DET_TOP)
    if why is not None:
        pytest.skip(f"XSI gate prerequisite missing: {why}")
    problems, jobs = _every_nit(32, 4, 5, seed=100 + FRONTIER[fmt])
    sim = CgDetectorSim(problems, jobs, K=4, fmt=FRONTIER[fmt])
    scenario = generate_tb(d, DET_TOP, sim.tb, sim)
    proc = run_xsi(d, DET_TOP)
    assert (
        proc.returncode == 0
    ), f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    assert (
        d / "xsi" / "vectors" / "out"
    ).exists(), "the XSI run produced no memory dump"
    check_xsi_outputs(d, scenario)
