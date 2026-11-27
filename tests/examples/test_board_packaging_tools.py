"""Manual, fresh S1/S2 Vitis 2025.1 and Vivado BD gates.

Run this file explicitly with both WAVEFLOW tool paths set. Physical acceptance
requires new overlays and the portable PYNQ hosts; these tests do not program PL.
"""
import json

import pytest

from examples.regmap.simp_fun_board import build_simp_fun_board_dag, board_config as simp_config
from examples.stream_inband.poly_board import build_poly_board_dag, board_config
from waveflow.build.ip_export import _sha

pytestmark = [pytest.mark.vitis, pytest.mark.vivado]


@pytest.mark.parametrize("name,factory,config", [
    ("simp_fun", build_simp_fun_board_dag, simp_config),
    ("poly", build_poly_board_dag, board_config),
])
def test_fresh_kernel_export_driver_and_bd(tmp_path, name, factory, config):
    results = factory().run(config(tmp_path), through="bd_validate", force=True)
    assert "bd_validate" in results, {n: r.traceback for n, r in results.items() if not r.success}
    assert all(r.success and not r.skipped for r in results.values()), {
        n: r.traceback for n, r in results.items() if not r.success}
    receipt = results["synthesize"].path("hls_synthesis_receipt")
    verification = json.loads(results["synthesize"].path("hls_verification").read_text())
    assert verification["passed"] and verification["synthesis_receipt_sha256"] == _sha(receipt)
    spec = results["control_abi"].object("hls_control_spec")
    host = json.loads(results["export_host_sim"].path("export_host_sim_result").read_text())
    assert host["passed"] and host["abi_sha256"] == spec.abi_sha256
    assert not host["board_execution_verified"]
    if name == "poly":
        assert "coeffs" not in spec.modeled_fields
        assert spec.field("error").width == 8
        assert verification["input_frame_words"] == [6, 100, 6]
        assert verification["output_frame_words"] == [1, 100]
        assert len(host["cases"]) == 3
    else:
        assert verification["csim"] == verification["cosim"] == 11
        assert [case["y"] for case in host["cases"]] == [11, 0, 11, 17]
    bd = json.loads(results["bd_validate"].path("bd_validation").read_text())
    assert bd["synthesis_performed"] is False
    assert bd["verdict"]["physical_hz"] == pytest.approx(100e6, rel=1e-6)
    assert len(bd["verdict"]["addresses"]) == (4 if name == "poly" else 1)
    assert "bitstream" not in results
