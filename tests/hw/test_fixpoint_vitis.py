"""Phase 3 (Vitis): the FixedField ap_fixed codegen compiles + runs under csim.

(a) DataArray[FixedField] serialization round-trips through generated array C++
    (the .range() bit-reinterpret conversions), reusing the arrayutils rig.
(b) A generated ap_fixed arithmetic kernel (full-precision mult + quantize-on-assign)
    compiles and computes bit-exactly for one config — a smoke of the conformance
    rig the Phase-4 milestone runs in full.

A failed csim is a real failure; we only skip when Vitis is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.schemas.fixedpoint.fixedpoint_build import conformance_for_case, div_cases
from examples.schemas.fixedpoint.kernels import render_binop
from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import gen_array_utils, read_array, write_array
from waveflow.hw.fixpoint import FixedField, from_real, mult, quantize, to_real
from waveflow.utils import fixputils
from waveflow.utils.fixputils import OMode, QMode
from waveflow.toolchain import toolchain

RESOURCE_DIR = Path(__file__).parent / "resources"
FIXEDPOINT_DIR = Path(__file__).resolve().parents[2] / "examples" / "schemas" / "fixedpoint"


def _run_tcl(tcl_path: Path, work_dir: Path, prefix: str) -> None:
    try:
        toolchain.run_vitis_hls(tcl_path, work_dir=work_dir)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"{prefix}\nrc={exc.returncode}\nstdout:\n{exc.stdout}\nstderr:\n{exc.stderr}")


@pytest.mark.vitis
@pytest.mark.parametrize("W,I,signed,q,o", [
    (8, 4, True, QMode.AP_TRN, OMode.AP_WRAP),
    (8, 4, False, QMode.AP_TRN, OMode.AP_WRAP),
    (16, 8, True, QMode.AP_RND, OMode.AP_SAT),
])
def test_fixedfield_array_csim_roundtrip(tmp_path, W, I, signed, q, o):  # noqa: E741
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    F = FixedField.specialize(W, I, signed=signed, q_mode=q, o_mode=o, include_dir="include")
    word_bw = 32
    lsb = 2.0 ** (-(W - I))
    lo = -(1 << (W - 1)) if signed else 0
    hi = (1 << (W - 1)) - 1 if signed else (1 << W) - 1
    reals = (np.linspace(lo, hi, 12).astype(np.int64) * lsb).astype(np.float64)
    da = from_real(reals, F)                            # quantize -> DataArray[F] (stored ints)
    stored = np.asarray(da)
    in_words = write_array(da, word_bw=word_bw)         # serialize the stored ints

    np.savetxt(tmp_path / "array_words.txt", np.asarray(in_words).astype(np.uint32), fmt="%u")
    cfg = BuildConfig(root_dir=tmp_path)
    header = gen_array_utils(F, [word_bw], cfg=cfg, streamutils_dir="include")
    StreamUtilsStep(output_dir="include").run(cfg)
    cpp = (RESOURCE_DIR / "arrayutils_roundtrip_test.cpp").read_text(encoding="utf-8")
    (tmp_path / "arrayutils_roundtrip_test.cpp").write_text(
        cpp.replace("__HEADER__", header.relative_to(tmp_path).as_posix())
        .replace("__NAMESPACE__", header.stem).replace("__WORD_BW__", str(word_bw))
        .replace("__ARRAY_LEN__", str(len(reals))).replace("__NWORDS__", str(np.asarray(in_words).shape[0])),
        encoding="utf-8")
    shutil.copy(RESOURCE_DIR / "arrayutils_roundtrip_run.tcl", tmp_path / "arrayutils_roundtrip_run.tcl")
    _run_tcl(tmp_path / "arrayutils_roundtrip_run.tcl", tmp_path, f"FixedField<{W},{I}> roundtrip")

    out_words = np.atleast_1d(np.loadtxt(tmp_path / "array_words_out.txt", dtype=np.uint32))
    out_stored = np.asarray(read_array(out_words, elem_type=F, word_bw=word_bw, shape=len(reals)))
    assert np.array_equal(out_words, np.asarray(in_words).astype(np.uint32))   # bits round-trip
    np.testing.assert_array_equal(out_stored, stored)                          # stored ints round-trip
    np.testing.assert_array_equal(to_real(da), reals)                          # and decode to the reals


@pytest.mark.vitis
def test_ap_fixed_mult_kernel_csim_bit_exact(tmp_path):
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    A = FixedField.specialize(8, 4)
    target = FixedField.specialize(8, 4, q_mode=QMode.AP_RND, o_mode=OMode.AP_SAT)
    a = from_real([1.5, -2.0, 0.5, 7.0, -3.5], A)
    b = from_real([2.0, 1.5, -1.0, 1.5, -2.0], A)
    py = quantize(mult(a, b), target)                       # full-precision mult + quantize
    fa, ft = A.get_format(), target.get_format()

    (tmp_path / "kernel.cpp").write_text(
        render_binop("*", A.cpp_type, fa.W, A.cpp_type, fa.W, target.cpp_type, ft.W), encoding="utf-8")
    shutil.copy(FIXEDPOINT_DIR / "run.tcl", tmp_path / "run.tcl")
    np.savetxt(tmp_path / "in_a.txt", np.asarray(fixputils.to_bits(np.asarray(a), fa.W)).astype(np.uint64), fmt="%u")
    np.savetxt(tmp_path / "in_b.txt", np.asarray(fixputils.to_bits(np.asarray(b), fa.W)).astype(np.uint64), fmt="%u")
    _run_tcl(tmp_path / "run.tcl", tmp_path, "ap_fixed mult kernel")

    vitis_bits = np.atleast_1d(np.loadtxt(tmp_path / "out_bits.txt", dtype=np.uint64))
    expected = np.asarray(fixputils.to_bits(np.asarray(py), ft.W)).astype(np.uint64)
    assert np.array_equal(vitis_bits, expected), (
        f"mult+quantize bits differ: vitis={vitis_bits} python={expected} (real={to_real(py)})")


DIV_CASES = div_cases()


def test_div_cases_cover_the_ac22_requirements():
    """plans/mimo_cg AC2.2: >= 1000 operand pairs, >= 3 formats, zero and tiny divisors, sat."""
    assert sum(len(c["expected"]) for c in DIV_CASES) >= 1000
    assert len({c["name"].split("_to_")[0] for c in DIV_CASES}) >= 3
    assert any(c["name"].endswith("_sat") for c in DIV_CASES)
    assert any(c["name"].endswith("_wrap") for c in DIV_CASES)


@pytest.mark.vitis
@pytest.mark.parametrize("case", DIV_CASES, ids=[c["name"] for c in DIV_CASES])
def test_ap_fixed_div_with_zero_guard_is_bit_exact(tmp_path, case):
    """``div`` == Vitis ``(b == 0) ? 0 : a / b``, bit for bit, exact and quantized in all modes."""
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    result = conformance_for_case(case, tmp_path)
    assert result["count_ok"], f"{case['name']}: Vitis emitted a different number of outputs."
    assert result["exact"], (
        f"{case['name']}: {len(result['mismatches'])} disagreement(s) between Python and Vitis; "
        f"the Python model is wrong (do NOT loosen). First few: {result['mismatches'][:5]}"
    )
