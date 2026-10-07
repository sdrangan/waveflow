"""Code generation for the streaming polynomial example: a body-only kernel.

The example's kernel is generated as a boundary -- prototype, every interface pragma, one
call to the hand-written body -- around ``poly_body_impl.tpp``.  The testbench is the
hand-written ``poly_tb.cpp``.  (plans/hook_first_flow.md, Stage 3.)
"""
from __future__ import annotations

from pathlib import Path

from examples.stream_inband.poly import PolyAccel
from examples.stream_inband.poly_build import build_poly_dag
from waveflow.build.build import BuildConfig
from waveflow.build.hwgen import cpp_kernel_name, kernel_signature
from waveflow.simulation.simulation import Simulation

POLY_ROOT = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"


def _gen(tmp_path: Path) -> tuple[str, str]:
    results = build_poly_dag().run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    assert results["gen_kernel"].success, results["gen_kernel"].message
    return ((tmp_path / "gen" / "poly.hpp").read_text(encoding="utf-8"),
            (tmp_path / "gen" / "poly.cpp").read_text(encoding="utf-8"))


def test_kernel_name_is_poly() -> None:
    assert cpp_kernel_name(PolyAccel) == "poly"


def test_signature_is_streams_and_status_only() -> None:
    """The coefficients arrive on the stream, so they are not a kernel argument (rule 2)."""
    sig = kernel_signature(PolyAccel(name="poly", sim=Simulation()))
    assert "coeffs" not in sig
    for arg in ("& s_in", "& m_out", "ap_uint<1>& halted", "ap_uint<8>& error",
                "ap_uint<16>& tx_id"):
        assert arg in sig, arg


def test_top_is_the_boundary_and_one_call_to_the_body(tmp_path: Path) -> None:
    hpp, cpp = _gen(tmp_path)
    assert "#pragma HLS INTERFACE axis port=s_in" in cpp
    assert "s_axilite port=coeffs" not in cpp
    assert "poly_impl::body(s_in, m_out, halted, error, tx_id);" in cpp
    assert "while" not in cpp                      # the loop is the body's, not extracted
    assert '#include "../poly_body_impl.tpp"' in hpp
    assert "ap_uint<1>& halted" in hpp             # status reaches the body by reference


def test_the_committed_body_is_used_not_a_stub(tmp_path: Path) -> None:
    """A build elsewhere copies the hand-written body before the generator could stub it."""
    _gen(tmp_path)
    built = (tmp_path / "poly_body_impl.tpp").read_text(encoding="utf-8")
    assert built == (POLY_ROOT / "poly_body_impl.tpp").read_text(encoding="utf-8")
    assert "TODO: implement body" not in built


def test_source_tree_is_hand_written_body_and_testbench() -> None:
    for name in ("poly_body_impl.tpp", "poly_tb.cpp", "run.tcl", "scenarios.py"):
        assert (POLY_ROOT / name).exists(), name
    assert not (POLY_ROOT / "poly_evaluate_impl.tpp").exists()
    build = (POLY_ROOT / "poly_build.py").read_text(encoding="utf-8")
    assert "PolyTBHls" not in build and "is_testbench=True" not in build
    tb = (POLY_ROOT / "poly_tb.cpp").read_text(encoding="utf-8")
    assert "wf::play_stream" in tb and "wf::record_stream" in tb


def test_param_supports_adds_the_64_bit_top(tmp_path: Path) -> None:
    """One templated body, two tops: poly (32-bit words) and poly_bw64 (64-bit words)."""
    hpp, cpp = _gen(tmp_path)
    assert "void poly_bw64(" in cpp and "axi4s_word<64>" in cpp
    assert cpp.count("poly_impl::body(s_in, m_out, halted, error, tx_id);") == 2
