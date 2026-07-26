"""Codegen-pipeline tests for the poly example (HlsCodegenStep wiring)."""
from __future__ import annotations

from pathlib import Path


def test_poly_cpp_kernel_name_is_poly():
    """PolyAccel overrides cpp_kernel_name to 'poly' (not 'poly_accel')."""
    from examples.stream_inband.poly import PolyAccel
    from waveflow.build.hwgen import cpp_kernel_name
    assert cpp_kernel_name(PolyAccel) == "poly"


def test_poly_codegen_step_extracts_and_writes(tmp_path: Path):
    """gen_kernel writes .hpp/.cpp into <root>/gen/ and the sticky .tpp at <root>/."""
    from examples.stream_inband.poly_build import build_poly_dag
    from waveflow.build.build import BuildConfig

    dag = build_poly_dag()
    results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    assert results["gen_kernel"].success, results["gen_kernel"].message
    gen_dir = tmp_path / "gen"
    assert (gen_dir / "poly.hpp").exists()
    assert (gen_dir / "poly.cpp").exists()
    # impl_dir="." → .tpp lands at the source-tree root, not under gen/.
    assert (tmp_path / "poly_evaluate_impl.tpp").exists()
    assert not (gen_dir / "poly_evaluate_impl.tpp").exists()


def test_poly_kernel_signature_has_raw_coeffs_array():
    """CoeffArray uses cpp_storage='raw': signature has 'float coeffs[4]', not 'CoeffArray& coeffs'."""
    from examples.stream_inband.poly import PolyAccel
    from waveflow.build.hwgen import kernel_signature
    from waveflow.simulation.simulation import Simulation

    comp = PolyAccel(name="poly", sim=Simulation())
    sig = kernel_signature(comp)
    assert "float coeffs[4]" in sig
    assert "CoeffArray& coeffs" not in sig


def test_poly_codegen_step_kernel_contains_raw_coeffs(tmp_path: Path):
    """End-to-end: generated poly.hpp contains 'coeffs[4]' in the kernel signature."""
    from examples.stream_inband.poly_build import build_poly_dag
    from waveflow.build.build import BuildConfig

    dag = build_poly_dag()
    results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    assert results["gen_kernel"].success, results["gen_kernel"].message
    hpp = (tmp_path / "gen" / "poly.hpp").read_text()
    assert "float coeffs[4]" in hpp
    assert "CoeffArray& coeffs" not in hpp


def test_poly_swap_over_state():
    """Headless verification of the final Phase 14 source-tree state.

    - All hand-written kernel + testbench C++ files are gone from the
      source tree (``poly.cpp`` / ``poly.hpp`` removed by Phase 12b;
      ``poly_tb.cpp`` removed by Phase 14).
    - ``poly_evaluate_impl.tpp`` (the sticky hand-written hook impl)
      still lives at the source-tree root.
    - ``poly_build.py`` carries no references to the legacy names and
      wires in both kernel + testbench HlsCodegenStep instances.
    """
    from pathlib import Path as _P

    poly_root = _P(__file__).resolve().parents[2] / "examples" / "stream_inband"

    # Hand-written kernel + testbench files are gone (now codegen'd).
    assert not (poly_root / "poly.cpp").exists()
    assert not (poly_root / "poly.hpp").exists()
    assert not (poly_root / "poly_tb.cpp").exists()

    # The sticky hand-written evaluate body is committed at the root.
    tpp = poly_root / "poly_evaluate_impl.tpp"
    assert tpp.exists()
    tpp_text = tpp.read_text(encoding="utf-8")
    assert "namespace poly" in tpp_text
    assert "evaluate" in tpp_text
    assert "eval_poly_horner" in tpp_text

    # poly_build.py: no legacy refs, both codegen steps wired in.
    build_text = (poly_root / "poly_build.py").read_text(encoding="utf-8")
    assert "handwritten_poly" not in build_text
    assert 'comp_class=PolyTBHls' in build_text
    assert 'is_testbench=True' in build_text
    assert 'FunctionalVerifyStep' in build_text


def test_poly_gen_hpp_uses_relative_impl_include(tmp_path: Path):
    """gen/poly.hpp must reference the .tpp via ``../poly_evaluate_impl.tpp``."""
    from examples.stream_inband.poly_build import build_poly_dag
    from waveflow.build.build import BuildConfig

    dag = build_poly_dag()
    results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    assert results["gen_kernel"].success, results["gen_kernel"].message
    hpp = (tmp_path / "gen" / "poly.hpp").read_text(encoding="utf-8")
    assert '#include "../poly_evaluate_impl.tpp"' in hpp


def test_poly_gen_hpp_includes_hook_schemas_and_utility_headers(tmp_path: Path):
    """Phase 12a verification: hook-referenced schemas and utility headers appear.

    - Blocker 1 fix: ``poly_cmd_hdr.h`` lands because ``evaluate`` takes
      ``cmd_hdr: PolyCmdHdr``.  (It also happens to appear as a kernel
      stmt output type, but this test asserts the include unconditionally.)
    - Blocker 2 fix: ``float32_array_utils.h`` lands because ``CoeffArray``
      (regmap field) is a ``DataArray[Float32]``, whose
      ``get_utility_includes`` returns the array-utils header path.
    """
    from examples.stream_inband.poly_build import build_poly_dag
    from waveflow.build.build import BuildConfig

    dag = build_poly_dag()
    results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    assert results["gen_kernel"].success, results["gen_kernel"].message
    hpp = (tmp_path / "gen" / "poly.hpp").read_text()

    assert '#include "include/poly_cmd_hdr.h"' in hpp
    assert '#include "include/float32_array_utils.h"' in hpp
