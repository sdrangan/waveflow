"""AC2.3 of plans/mimo_cg/mimo_cg_paper_sims.md: the Python golden CG == its C++ reference.

The ``-m vitis`` test runs every case set through Vitis HLS C-simulation for xczu48dr and
requires every stored bit (X after every iteration, α, β) to match.  A failed comparison
means one of the two is wrong: find which, never loosen the comparison.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.mimo_cg.mimo_cg_conformance import (
    CASES_PER_SET,
    CPP_DIR,
    FORMAT_SETS,
    STRESS_FORMATS,
    _problems,
    build_case_set,
    case_set_specs,
    conformance_for_case_set,
)
from examples.mimo_cg.mimo_cg_fixed import cg_fixed
from waveflow.toolchain import toolchain

SPECS = case_set_specs()


def test_case_sets_cover_ac23():
    """>= 3 format sets, >= 50 random cases each, both residual forms, a zero-residual case."""
    assert len(FORMAT_SETS) >= 3 and CASES_PER_SET >= 50
    for name in FORMAT_SETS:
        forms = {s.explicit for s in SPECS if s.formats == FORMAT_SETS[name]}
        assert forms == {False, True}, name
    problems = _problems(SPECS[0])
    assert len(problems) == CASES_PER_SET + 1
    assert np.all(problems[-1][1][:, 2] == 0)  # the zero-residual case
    assert "xczu48dr-ffvg1517-2-e" in (CPP_DIR / "run.tcl").read_text()
    for spec in SPECS:
        spec.formats.intermediates(spec.K)  # every set fits the 64-bit cap


def test_zero_residual_case_takes_both_zero_guards():
    spec = SPECS[0]
    A, B, M = _problems(spec)[-1]
    regs = []
    cg_fixed(
        A, B, spec.nit, spec.formats, scale=M, on_iteration=lambda n, r: regs.append(r)
    )
    assert all(r["alpha"][0][0, 2] == 0 and r["beta"][0][0, 2] == 0 for r in regs)
    assert all(r["ps"][0][0, 2] == 0 and r["rz"][0][0, 2] == 0 for r in regs)


def test_testbench_declares_the_golden_formats():
    cs = build_case_set(SPECS[0])
    assert "typedef ap_fixed<28, 3, AP_RND, AP_SAT> a_t;" in cs["main"]
    assert "typedef ap_fixed<29, 5, AP_RND, AP_SAT> alpha_t;" in cs["main"]
    assert len(cs["expected"]) == len(cs["labels"])


@pytest.mark.vitis
@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_python_golden_matches_cpp_reference_in_csim(tmp_path: Path, spec):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; cannot run the CG conformance.")
    result = conformance_for_case_set(build_case_set(spec), tmp_path)
    assert result[
        "count_ok"
    ], f"{spec.name}: C-sim emitted a different number of words."
    assert result["exact"], (
        f"{spec.name}: {len(result['mismatches'])} mismatching words; find which side is wrong "
        f"(do NOT loosen). First few: {result['mismatches'][:5]}"
    )


def test_stress_set_saturates_alpha_and_beta():
    """The saturation paths must be exercised: the stress set saturates alpha and beta."""
    spec = next(s for s in SPECS if s.name == "stress_sat_recurrence_k16")
    assert spec.formats == STRESS_FORMATS
    counts = {"alpha": 0, "beta": 0}
    for A, B, M in _problems(spec):
        regs = []
        cg_fixed(
            A,
            B,
            spec.nit,
            spec.formats,
            scale=M,
            on_iteration=lambda n, r, regs=regs: regs.append(r),
        )
        for r in regs:
            for name in counts:
                fmt = getattr(spec.formats, name)
                hi, lo = (1 << (fmt.W - 1)) - 1, -(1 << (fmt.W - 1))
                counts[name] += int(np.sum((r[name][0] == hi) | (r[name][0] == lo)))
    assert counts["alpha"] >= 1 and counts["beta"] >= 10, counts
