"""Phase 4 milestone: Python ``ComplexField`` == Vitis complex, bit-exact.

The ``-m vitis`` test runs every conformance case (round-trip + cmult / cadd / csub /
conj, per inner: fixed vs ``std::complex<ap_fixed>``, int vs the ``wf_cint`` struct, float
vs ``std::complex<float>``/``<double>``) in Vitis C-sim and asserts the emitted stored
bits equal the Python ``DataArray[ComplexField]`` bits with zero LSB disagreement.  A
failed csim is a real failure -- only skip when Vitis is absent.  The float-complex
multiply is the empirically-confirmed edge.
"""
import pytest

from examples.schemas.complex.complex_build import build_cases, conformance_for_case
from waveflow.toolchain import toolchain

CASES = build_cases()


def test_cases_cover_every_inner_and_op():
    names = {c["name"] for c in CASES}
    # round-trip per inner kind
    for n in ("roundtrip_s8_4", "roundtrip_u8_4", "roundtrip_i8", "roundtrip_f32", "roundtrip_f64"):
        assert n in names
    # cmult headline: fixed (signed), int, float (the edge); NOT unsigned fixed
    assert "cmult_s8_4" in names and "cmult_i8" in names and "cmult_f32" in names
    assert "cmult_u8_4" not in names                     # cmult is signed-inner only
    # cmult3 (the three-multiply form): fixed and int, never float (it rounds differently there)
    assert "cmult3_s8_4" in names and "cmult3_i8" in names and "cmult3_f32" not in names
    # cadd / csub / conj coverage
    assert {"cadd_s8_4", "csub_s8_4", "conj_s8_4", "cadd_u8_4"} <= names
    assert {"cadd_i16", "csub_i16", "conj_i16"} <= names
    assert {"cadd_f64", "csub_f64", "conj_f64"} <= names


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
        f"Vitis complex — the Python model is wrong, fix it (do NOT loosen). "
        f"First few: {result['mismatches'][:5]}")
