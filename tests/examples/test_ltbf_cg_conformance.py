"""AC3 of plans/mimo_cg/ltbf_cg_dse.md: the fixed-point LTBF CG == its C++ reference.

The ``-m vitis`` test runs every case set through Vitis HLS C-simulation for xczu48dr and
requires every stored bit (X after every iteration, alpha, beta) to match.  A failed comparison
means one of the two is wrong: find which, never loosen the comparison.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.ltbf_cg.ltbf_conformance import (
    CASES_PER_SET,
    NIT,
    SRC_DIR,
    ZERO_COL,
    build_case_set,
    case_set_formats,
    case_set_specs,
    conformance_for_case_set,
    golden,
    problems,
)
from waveflow.toolchain import toolchain

SPECS = case_set_specs()


def test_case_sets_cover_ac3():
    """>= 20 random cases per regime and Q form at N_rx <= 64, a zero-divisor case, a 24-bit set."""
    assert CASES_PER_SET >= 20
    combos = {(s.regime, s.form) for s in SPECS}
    assert combos == {
        (1, "explicit"),
        (1, "factored"),
        (2, "explicit"),
        (2, "factored"),
    }
    assert all(s.n_rx <= 64 for s in SPECS)
    assert any(s.W == 24 for s in SPECS)
    for spec in SPECS:
        probs = problems(spec)
        assert len(probs) == CASES_PER_SET + 1
        assert not np.any(probs[-1][1][:, ZERO_COL])  # the zero-divisor case
    assert "xczu48dr-ffvg1517-2-e" in (SRC_DIR / "run.tcl").read_text()


@pytest.mark.parametrize("spec", SPECS[:4], ids=[s.name for s in SPECS[:4]])
def test_zero_divisor_case_takes_both_zero_guards(spec):
    ha, b = problems(spec)[-1]
    regs = golden(spec, ha, b)
    assert len(regs) == NIT
    for r in regs.values():
        assert r["alpha"][ZERO_COL] == 0 and r["beta"][ZERO_COL] == 0
        assert not np.any(r["xr"][:, ZERO_COL]) and not np.any(r["xi"][:, ZERO_COL])


def test_stress_set_saturates_at_both_rails():
    """The saturation paths must be exercised: in the stress set H and X hit both rails (the most
    negative H is the adjoint's sign corner), alpha and beta the top one, and some registers have
    zero or negative integer bits."""
    from examples.ltbf_cg import ltbf_fixed as lf

    spec = next(s for s in SPECS if s.name.endswith("_sat"))
    f = case_set_formats(spec)
    c = f.cg
    assert min(fmt.int_bits for fmt in (f.H, c.X, c.alpha)) < 0
    counts: dict[str, list[int]] = {}

    def rails(name, arr, fmt):
        top, bot = (1 << (fmt.W - 1)) - 1, -(1 << (fmt.W - 1))
        n = counts.setdefault(name, [0, 0])
        n[0] += int(np.sum(arr >= top))
        n[1] += int(np.sum(arr <= bot))

    for ha, b in problems(spec):
        hr, hi = lf.to_stored(ha, f.H)
        rails("H", np.concatenate([hr.ravel(), hi.ravel()]), f.H)
        for r in golden(spec, ha, b).values():
            rails("X", np.concatenate([r["xr"].ravel(), r["xi"].ravel()]), c.X)
            rails("alpha", r["alpha"], c.alpha)
            rails("beta", r["beta"], c.beta)
    assert min(counts["H"]) > 0 and min(counts["X"]) > 0, counts
    assert counts["alpha"][0] > 0 and counts["beta"][0] > 0, counts


def test_testbench_declares_the_golden_formats():
    cs = build_case_set(SPECS[0])
    f = case_set_formats(SPECS[0])
    assert (
        f"typedef ap_fixed<{f.H.W}, {f.H.int_bits}, AP_RND, AP_SAT> h_t;" in cs["main"]
    )
    assert "static const bool EXPLICIT = true;" in cs["main"]
    assert len(cs["expected"]) == len(cs["labels"])


@pytest.mark.vitis
@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_python_golden_matches_cpp_reference_in_csim(tmp_path: Path, spec):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; cannot run the LTBF CG conformance.")
    result = conformance_for_case_set(build_case_set(spec), tmp_path)
    assert result[
        "count_ok"
    ], f"{spec.name}: C-sim emitted a different number of words."
    assert result["exact"], (
        f"{spec.name}: {len(result['mismatches'])} mismatching words; find which side is wrong "
        f"(do NOT loosen). First few: {result['mismatches'][:5]}"
    )
