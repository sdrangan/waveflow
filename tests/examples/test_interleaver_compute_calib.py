"""tests/examples/test_interleaver_compute_calib.py — the custom compute's direct loop-model fit.

The interleaver's ``il_compute`` gather is a CUSTOM kernel, so its timing is fit by the design (not
shipped with the platform). This pins the machinery — fit a line from an ``(n, cycles)`` sweep, save it,
and have ``IlComputeInband`` load it — with synthetic points, toolchain-free. (The real cycle counts, a
clean ``cycles = n``, are measured by ``measure_compute_spans.py`` and live in ``calibrate_compute.N_TO_CYCLES``.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "examples" / "interleaver"))


def test_fit_load_predict_roundtrip(tmp_path):
    from calibrate_compute import fit_compute_model
    from interleaver_inband import IlComputeInband
    from waveflow.simulation.simulation import Simulation

    # cycles = 1.5625·n (latency 0, a clean line through the three points), keyed on element count n
    pts = {64: 100.0, 128: 200.0, 256: 400.0}
    out = fit_compute_model(pts, tmp_path)
    assert Path(out).exists()

    # The loaded model must reproduce the fit — predicted on the element count n.
    c = IlComputeInband(name="c", sim=Simulation(), mem_dwidth=64, n=256, calib_dir=str(tmp_path))
    assert c.n == 256
    assert c.compute_timing.predict_feat({"n": 256}) == pytest.approx(400.0, abs=0.1)
    assert c.compute_timing.predict_feat({"n": 128}) == pytest.approx(200.0, abs=0.1)


def test_seed_used_without_calib(tmp_path):
    from interleaver import IL_COMPUTE_II_SEED, IL_COMPUTE_LATENCY_SEED
    from interleaver_inband import IlComputeInband
    from waveflow.simulation.simulation import Simulation

    # No calib_dir -> the seed loop model (latency + ii·(n−1)), keyed on the element count n.
    c = IlComputeInband(name="c", sim=Simulation(), mem_dwidth=64, n=256)
    expect = IL_COMPUTE_LATENCY_SEED + IL_COMPUTE_II_SEED * (256 - 1)
    assert c.compute_timing.predict_feat({"n": 256}) == pytest.approx(expect, abs=0.1)


def test_fit_needs_two_sizes(tmp_path):
    from calibrate_compute import fit_compute_model

    with pytest.raises(ValueError, match="2 distinct sizes"):
        fit_compute_model({128: 200.0}, tmp_path)
