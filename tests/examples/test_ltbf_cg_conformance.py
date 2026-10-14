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


def test_stress_set_saturates():
    """The saturation paths must be exercised: the stress set saturates beta and H."""
    spec = next(s for s in SPECS if s.name.endswith("_sat"))
    f = case_set_formats(spec)
    beta_top = (1 << (f.cg.beta.W - 1)) - 1
    h_top = (1 << (f.H.W - 1)) - 1
    n_beta = n_h = 0
    from examples.ltbf_cg import ltbf_fixed as lf

    for ha, b in problems(spec):
        hr, hi = lf.to_stored(ha, f.H)
        n_h += int(np.sum(np.abs(hr) >= h_top) + np.sum(np.abs(hi) >= h_top))
        for r in golden(spec, ha, b).values():
            n_beta += int(np.sum(np.abs(r["beta"]) >= beta_top))
    assert n_beta > 0 and n_h > 0


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
