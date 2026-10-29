"""AC6 of plans/mimo_cg/ltbf_cg_dse.md: the A53's eigendecomposition step and its calibration.

``examples/ltbf_dse/cpu/ltbf_eig.c`` and its twin agree bit for bit (outputs and work counters) on
the host build, and on gem5 (``-m gem5``: the ground-truth gate).  The calibration corpus and the
fitted model are local (``examples/ltbf_dse/data/a53_ltbf/``); where they exist, the model's
one-time test score is checked against AC6's bound.

**Provenance.** Copied from ``paper/cg_dse`` (``examples/ltbf_cg/``, ``ba757884``) by
``plans/ltbf_dse/ltbf_dse.md`` step 0.5; the ``plans/mimo_cg/ltbf_cg_dse.md`` it cites lives on that
branch (``git show paper/cg_dse:plans/mimo_cg/ltbf_cg_dse.md``).
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from examples.ltbf_dse import ltbf_eig as le

POINTS = (
    *le.KERNEL.smoke,
    {"n": 200, "r": 2, "rank": 2, "seed": 201},
    {"n": 64, "r": 1, "rank": 4, "seed": 5},
)


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs a host gcc")
@pytest.mark.parametrize(
    "point", POINTS, ids=lambda p: "-".join(f"{k}{v}" for k, v in p.items())
)
def test_twin_matches_the_host_build(point, tmp_path):
    assert le.ltbf_eig(**point) == le.host_run(point, tmp_path)


def test_jacobi_matches_numpy():
    """The top-r eigenvectors and G = (A Phi)^H match numpy's eigh (up to each vector's phase)."""
    rng = np.random.default_rng(0)
    a = rng.standard_normal((32, 8)) + 1j * rng.standard_normal((32, 8))
    h = rng.standard_normal((32, 8)) + 1j * rng.standard_normal((32, 8))

    def to(x):
        return [[[float(v.real), float(v.imag)] for v in row] for row in x]

    g, lam, _, _ = le._eig_project(to(a), to(h), 32, 2)
    m = h.conj().T @ a
    w, v = np.linalg.eigh(0.5 * (m + m.conj().T))
    assert lam == pytest.approx(w.max(), rel=1e-13)
    want = (a @ v[:, np.argsort(w)[::-1][:2]]).conj().T
    got = np.array([[complex(*x) for x in row] for row in g])
    for j in range(2):
        cos = abs(np.vdot(want[j], got[j])) / (
            np.linalg.norm(want[j]) * np.linalg.norm(got[j])
        )
        assert cos == pytest.approx(1.0, abs=1e-12)


def test_counters_of_a_real_ue_drive_the_model():
    """What the DSE prices per UE: n, r and the Jacobi work of that UE's own M."""
    rng = np.random.default_rng(1)
    h = rng.standard_normal((64, 8)) + 1j * rng.standard_normal((64, 8))
    c = le.eig_counters(h * 0.5, h, 2)
    assert (
        c["n"] == 64
        and c["r"] == 2
        and c["n_rot"] > 0
        and 1 <= c["n_sweeps"] <= le.MAX_SWEEPS
    )


@pytest.mark.skipif(
    not (le.DATA / "model.json").is_file(), reason="local calibration not run"
)
def test_the_fitted_model_meets_ac6_on_the_test_points():
    errors = json.loads((le.DATA / "model.json").read_text())["errors"]
    assert errors["test"]["points"] >= 8
    assert errors["test"]["median"] <= 0.10, errors


@pytest.mark.gem5
@pytest.mark.parametrize("point", le.KERNEL.smoke, ids=lambda p: f"n{p['n']}-r{p['r']}")
def test_gem5_run_matches_the_twin(point):
    """The ground truth: the binary gem5 runs prints exactly the twin's outputs and counters."""
    run = le.runner()
    why = run.unavailable()
    if why:
        pytest.skip(f"gem5 unavailable: {why}")
    row = run.measure(le.KERNEL, point, empty_cycles=0.0, smoke=True)
    assert row["output_matches_twin"], row
    assert row["cycles"] > 0
