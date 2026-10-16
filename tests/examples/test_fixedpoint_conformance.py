"""Phase 4 milestone: Python integer-backed fixed-point == Vitis ap_fixed, bit-exact.

The ``-m vitis`` test runs every conformance case (quantization over the curated
configs/modes, plus mult / add / quantize / sum-of-products) in Vitis C-sim and
asserts the emitted bits equal the Python DataArray[FixedField] bits with zero LSB
disagreement.  A failed csim is a real failure — only skip when Vitis is absent.
"""
import pytest

from examples.schemas.fixedpoint.fixedpoint_build import build_cases, conformance_for_case
from waveflow.toolchain import toolchain

CASES = build_cases()


def test_cases_cover_quantization_and_all_arithmetic_ops():
    names = {c["name"] for c in CASES}
    assert any(n.startswith("mult_") for n in names)
    assert any(n.startswith("add_") for n in names)
    assert any(n.startswith("quant_prod_to_s8_4") for n in names)     # requantize
    assert "dot_s24_12_n16" in names                                   # sum-of-products
    assert sum(n.startswith("div_") for n in names) >= 5            # division, zero-guarded
    # every curated quantization config x mode (6 widths x 4 modes = 24)
    quant = [n for n in names if n.startswith("quant_s") or n.startswith("quant_u")]
    assert len(quant) == 24


def test_every_case_has_expected_bits():
    for c in CASES:
        assert c["expected"] and all(isinstance(b, int) and b >= 0 for b in c["expected"])


@pytest.mark.vitis
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_python_matches_vitis_bit_exact(tmp_path, case):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found; cannot run bit-exact conformance.")
    result = conformance_for_case(case, tmp_path)
    assert result["count_ok"], f"{case['name']}: Vitis emitted a different number of outputs."
    assert result["exact"], (
        f"{case['name']}: {len(result['mismatches'])} LSB disagreement(s) between Python and "
        f"Vitis ap_fixed — the Python model is wrong, fix it (do NOT loosen). "
        f"First few: {result['mismatches'][:5]}")
