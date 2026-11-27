"""Opt-in real S0d acceptance. No tool mocks and no application imports."""
from dataclasses import replace
import json
import shutil

import pytest

from waveflow.board import Board, ClockRequest
from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.board_steps import (
    ExportIpStep, HlsControlSpecStep, HostDriverGenStep, BdGenStep,
    BdValidateStep, BitstreamStep, CollectOverlayStep,
)
from waveflow.build.ip_export import IpExportSpec, synthesize_for_board, _hash_tree
from waveflow.build.vivado_board import validate_bd, prepare_bd
from tests.board.control_abi_helpers import FIXTURES, intent
from tests.board.vivado_helpers import minimal_design

pytestmark = [pytest.mark.vivado, pytest.mark.vitis]


@pytest.fixture(scope="module", autouse=True)
def real_tools_opt_in(request):
    explicit_file = any("test_vivado_board_build.py" in str(a) for a in request.config.args)
    if not explicit_file and "vivado" not in request.config.option.markexpr:
        pytest.skip("S0d real tool builds are opt-in: select this file or -m vivado")


@pytest.fixture(scope="module")
def real_bd(tmp_path_factory):
    # These explicitly selected acceptance tests fail on missing tools, never silently skip.
    root = tmp_path_factory.mktemp("s0d_real")
    config = BuildConfig(root_dir=root, board=Board.rfsoc4x2(), clock_request=ClockRequest("ps_pl0", 100e6, 100e6),
                         vitis_version="2025.1")
    receipt = synthesize_for_board(config, top="scalar_probe",
        inputs={"scalar_probe.cpp": FIXTURES / "scalar_probe.cpp"}, sources=("scalar_probe.cpp",))
    before = _hash_tree(receipt.parent)
    dag = BuildDag()
    dag.add(SourceStep(artifact="hls_synthesis_receipt", path=receipt))
    dag.add(ExportIpStep(name="export", spec=IpExportSpec(name="scalar_probe")))
    dag.add(HlsControlSpecStep(name="abi", regmap=intent("scalar_probe")[0]))
    dag.add(HostDriverGenStep(name="driver"))
    dag.add(BdGenStep(name="bd", design=minimal_design()))
    dag.add(BdValidateStep(name="validate"))
    results = dag.run(config, through="validate", force=["export", "abi", "bd", "validate"])
    for name in ("export", "abi", "bd", "validate"):
        assert results[name].success, results[name].traceback
        assert not results[name].skipped
    assert _hash_tree(receipt.parent) == before
    return config, results, receipt, before


def test_real_minimal_bd_and_relocation(real_bd, tmp_path):
    config, results, receipt, before = real_bd
    path = results["validate"].path("bd_validation")
    r = json.loads(path.read_text())
    assert r["verdict"]["physical_hz"] == pytest.approx(100e6, rel=1e-6)
    assert r["verdict"]["addresses"][0]["offset"] == 0xA1000000
    assert r["synthesis_performed"] is False
    relocated = tmp_path / "fresh path with spaces"
    shutil.copytree(results["bd"].path("bd_inputs").parent, relocated)
    rerun = validate_bd(config, relocated / "bd_inputs.json", work_dir=tmp_path / "validation")
    assert json.loads(rerun.read_text())["verdict"] == r["verdict"]
    assert _hash_tree(receipt.parent) == before


@pytest.mark.parametrize("fault", ["clock", "reset", "address"])
def test_real_structural_rejections(real_bd, tmp_path, fault):
    config, results, _, _ = real_bd
    design = minimal_design()
    source = design.template.read_text()
    if fault == "clock":
        source = source.replace("double($WF_REQUEST_HZ) / 1e6", "200.0")
    elif fault == "reset":
        source = source.replace("[get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]",
                                "[get_bd_pins one/dout] [get_bd_pins reset/ext_reset_in]")
    else:
        # The template preassigns a conflicting window; the expected map must not be accepted.
        source += "\nassign_bd_address -offset 0xA2000000 -range 0x10000 -target_address_space [get_bd_addr_spaces ps/Data] [get_bd_addr_segs probe/s_axi_control/Reg]\n"
    template = tmp_path / "fault.tcl"
    template.write_text(source)
    inputs = prepare_bd(config, results["export"].path("ip_manifest"),
                        results["abi"].object("hls_control_spec"), replace(design, template=template),
                        work_dir=tmp_path / "inputs")
    with pytest.raises((RuntimeError, ValueError)):
        validate_bd(config, inputs, work_dir=tmp_path / "validation")
    assert not list((tmp_path / "validation").rglob("bd_validation.json"))
    expected = {"clock": "physical clock differs from requested profile",
                "reset": "unconnected pin: ps/pl_resetn0",
                "address": "template must not preassign the declared address space"}[fault]
    logs = list((tmp_path / "validation").rglob("tool_output.log"))
    assert len(logs) == 1 and "WAVEFLOW_ERROR: " + expected in logs[0].read_text()


def test_real_bitstream_overlay(real_bd, tmp_path):
    config, results, receipt, before = real_bd
    # Keep synthesis and earlier runs intact. BitstreamStep starts a second clean Vivado project.
    dag = BuildDag()
    dag.add(SourceStep(artifact="bd_validation", path=results["validate"].path("bd_validation")))
    from waveflow.build.host_driver import generate_host_driver
    driver = generate_host_driver(results["abi"].object("hls_control_spec"), tmp_path / "driver")
    dag.add(SourceStep(artifact="host_driver_manifest", path=driver.manifest))
    dag.add(BitstreamStep(name="bitstream"))
    dag.add(CollectOverlayStep(name="overlay"))
    built = dag.run(config, through="overlay", force=["bitstream", "overlay"])
    for name in ("bitstream", "overlay"):
        assert built[name].success, built[name].traceback
        assert not built[name].skipped
    path = built["overlay"].path("overlay_manifest")
    manifest = json.loads(path.read_text())
    assert manifest["implementation"]["timing_passed"]
    assert manifest["implementation"]["drc_passed"]
    assert manifest["board_execution_verified"] is False
    assert manifest["schema_version"] == 2
    assert {p.name for p in path.parent.iterdir()} == {
        "s0d_control.bit", "s0d_control.hwh", "driver.py", "manifest.json"}
    for ext in ("bit", "hwh"):
        assert (path.parent / ("s0d_control." + ext)).stat().st_size > 0
    implementation = built["bitstream"].path("bitstream_manifest").parent
    assert (implementation / "artifacts/design.xsa").stat().st_size > 0
    assert _hash_tree(receipt.parent) == before
    print("S0D_OVERLAY_MANIFEST=" + str(path))
