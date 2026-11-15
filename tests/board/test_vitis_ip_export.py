"""Opt-in S0b acceptance using real Vitis/Vivado and standalone C++ fixtures.

No application imports, PYNQ dependency, BD construction, or implementation.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from waveflow.board import Board
from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.board_steps import ExportIpStep
from waveflow.build.ip_export import IpExportSpec, synthesize_for_board
from waveflow.toolchain import find_vitis_path, find_vivado_path, run_vivado, tool_version

FIXTURES = Path(__file__).parent / "fixtures" / "ip_export"


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module", params=[
    ("ps_control", "axilite_probe"), ("rf_axis", "axilite_probe"), ("ps_control", "wire_probe"),
], ids=["axilite-100MHz", "axilite-250MHz", "no-control-100MHz"])
def real_package(request, tmp_path_factory):
    exe = find_vitis_path()
    if exe is None:
        pytest.skip("Vitis HLS not installed; S0b real export acceptance is NOT verified")
    board = Board.rfsoc4x2()
    assert tool_version(exe, "--version") == board.vivado_version
    profile, top = request.param
    root = tmp_path_factory.mktemp("s0b_" + top + "_" + profile)
    config = BuildConfig(root_dir=root, board=board, clock_profile=profile, vitis_version="2025.1")
    receipt = synthesize_for_board(
        config, top=top, inputs={top + ".cpp": FIXTURES / (top + ".cpp")},
        sources=(top + ".cpp",),
    )
    before = tree_hashes(receipt.parent)
    spec = IpExportSpec(name=top, required_interfaces=("s_axi_control",) if top == "axilite_probe" else ())
    dag = BuildDag()
    dag.add(SourceStep(artifact="hls_synthesis_receipt", path=receipt))
    dag.add(ExportIpStep(name="export", spec=spec))
    result = dag.run(config, through="export", force=["export"])["export"]
    assert result.success, result.traceback
    assert not result.skipped
    assert tree_hashes(receipt.parent) == before, "export changed the source solution"
    return config, spec, receipt, result


@pytest.mark.vitis
def test_real_rfsoc4x2_export(real_package):
    config, spec, receipt, result = real_package
    manifest_path = result.path("ip_manifest")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_level"] == "hls_ip_export"
    assert manifest["vlnv"] == spec.vlnv
    assert manifest["target"]["part"] == "xczu48dr-ffvg1517-2-e"
    assert manifest["target"]["hls_target_hz"] == config.clk_freq
    assert manifest["target"]["hls_period_ns"] == pytest.approx(1e9 / config.clk_freq)
    assert manifest["tool_version"] == "2025.1"
    assert manifest["ports"] and manifest["referenced_files"]
    assert manifest["archive_sha256"] == hashlib.sha256(result.path("ip_archive").read_bytes()).hexdigest()
    component = result.path("ip_directory") / "component.xml"
    assert manifest["component_xml_sha256"] == hashlib.sha256(component.read_bytes()).hexdigest()
    assert manifest["synthesis_receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert manifest["package_hashes"] == tree_hashes(result.path("ip_directory"))
    if spec.required_interfaces:
        assert "s_axi_control" in manifest["interfaces"]
        assert (manifest_path.parent / manifest["control_header"]).is_file()
        assert manifest["control_header_sha256"]
    else:
        assert manifest["control_header"] is None
        assert manifest["control_header_sha256"] is None
    if config.clock_profile == "rf_axis":
        assert manifest["target"]["clock"]["profile"]["physical_hz"] == 245.76e6
        assert manifest["target"]["hls_period_ns"] == 4.0


@pytest.mark.vitis
@pytest.mark.vivado
def test_relocated_package_is_discovered_by_vivado(real_package, tmp_path):
    config, spec, receipt, result = real_package
    exe = find_vivado_path()
    if exe is None:
        pytest.skip("Vivado not installed; optional relocated catalog check not performed")
    assert tool_version(exe, "-version") == config.board.vivado_version
    # The catalog sees ONLY the relocated directory, not the HLS project or ZIP.
    shutil.copytree(result.path("ip_directory"), tmp_path / "relocated_ip")
    tcl = tmp_path / "catalog.tcl"
    tcl.write_text(f"""if {{[catch {{
    create_project -in_memory -part {{{config.part}}}
    set_property ip_repo_paths [list [file normalize relocated_ip]] [current_project]
    update_ip_catalog
    set defs [get_ipdefs -all {{{spec.vlnv}}}]
    if {{[llength $defs] != 1}} {{error "expected exactly one relocated IP definition"}}
    file mkdir generated
    create_ip -vlnv {{{spec.vlnv}}} -module_name catalog_probe -dir generated
    if {{[llength [get_ips catalog_probe]] != 1}} {{error "IP could not be instantiated"}}
}} message]}} {{
    puts stderr "WAVEFLOW_ERROR: $message"
    exit 1
}}
puts "WAVEFLOW_IP_CATALOG_OK"
exit 0
""", encoding="utf-8")
    completed = run_vivado(tcl, tmp_path, required_version=config.board.vivado_version)
    assert "WAVEFLOW_IP_CATALOG_OK" in completed.stdout.splitlines()
