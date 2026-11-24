"""S0c DAG and evidence failure paths. Artifact envelopes here are test doubles."""
from dataclasses import replace
import json
import shutil

import pytest

from waveflow.board import Board
from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.board_steps import HlsControlSpecStep, HostDriverGenStep
from waveflow.build.hls_control import control_spec_from_export
from waveflow.build.ip_export import _board_target, _hash_tree, _sha
from tests.board.control_abi_helpers import FIXTURES, intent
from tests.board.clock_helpers import resolved_config


@pytest.fixture
def exported(tmp_path):
    config = resolved_config(tmp_path)
    root = tmp_path / "export"
    (root / "ip").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "scalar_probe/component.xml", root / "ip/component.xml")
    shutil.copyfile(FIXTURES / "scalar_probe/control.h", root / "control.h")
    (root / "ip.zip").write_bytes(b"unit-test envelope, not a real export")
    for file in ("export.tcl", "synthesize.tcl"):
        (root / file).write_text("# unit-test envelope\n")
    target = _board_target(config)
    receipt = {"target": target, "top": "scalar_probe", "input_hashes": {"source.cpp": "test"}}
    (root / "synthesis_receipt.json").write_text(json.dumps(receipt))
    (root / "csynth.xml").write_text(f'''<profile><ReportVersion><Version>2025.1</Version></ReportVersion>
<UserAssignments><Part>{config.part}</Part><TopModelName>scalar_probe</TopModelName>
<TargetClockPeriod>10.00</TargetClockPeriod><FlowTarget>vivado</FlowTarget></UserAssignments></profile>''')
    manifest = {
        "schema_version": 1, "validation_level": "hls_ip_export", "target": target,
        "tool_version": "2025.1", "vlnv": "waveflow:hls:scalar_probe:1.0", "top": "scalar_probe",
        "input_hashes": receipt["input_hashes"],
        "ip_directory": "ip", "package_hashes": _hash_tree(root / "ip"),
        "archive": "ip.zip", "archive_sha256": _sha(root / "ip.zip"),
        "csynth_report": "csynth.xml", "csynth_report_sha256": _sha(root / "csynth.xml"),
        "synthesis_receipt_sha256": _sha(root / "synthesis_receipt.json"),
        "synthesis_tcl_sha256": _sha(root / "synthesize.tcl"), "export_tcl_sha256": _sha(root / "export.tcl"),
        "control_header": "control.h", "control_header_sha256": _sha(root / "control.h"),
        "component_xml_sha256": _sha(root / "ip/component.xml"),
    }
    path = root / "ip_manifest.json"
    path.write_text(json.dumps(manifest))
    return config, path


def run_steps(config, path, regmap=None):
    dag = BuildDag()
    dag.add(SourceStep(artifact="ip_manifest", path=path))
    dag.add(HlsControlSpecStep(name="abi", regmap=regmap or intent("scalar_probe")[0]))
    dag.add(HostDriverGenStep(name="driver"))
    return dag.run(config, through="driver", force=["abi", "driver"])


def test_dag_publishes_verified_spec_and_portable_driver(exported):
    config, path = exported
    before = _hash_tree(path.parent)
    results = run_steps(config, path)
    assert results["abi"].success, results["abi"].traceback
    assert results["driver"].success, results["driver"].traceback
    assert not results["abi"].skipped and not results["driver"].skipped
    spec = results["abi"].object("hls_control_spec")
    record = json.loads(results["abi"].path("hls_control_manifest").read_text())
    assert record["abi_sha256"] == spec.abi_sha256
    assert record["ip_manifest_sha256"] == _sha(path)
    assert _hash_tree(path.parent) == before
    old_driver = results["driver"].path("host_driver")
    old_bytes = old_driver.read_bytes()
    again = run_steps(config, path)
    assert again["driver"].path("host_driver") != old_driver
    assert old_driver.read_bytes() == old_bytes


@pytest.mark.parametrize("filename", ["control.h", "ip/component.xml", "ip.zip", "csynth.xml",
                                       "synthesis_receipt.json", "synthesize.tcl", "export.tcl"])
def test_stale_evidence_stops_before_driver(exported, filename):
    config, path = exported
    artifact = path.parent / filename
    artifact.write_bytes(artifact.read_bytes() + b"\nchanged\n")
    results = run_steps(config, path)
    assert not results["abi"].success
    assert not (config.root_dir / "board_build/control").exists()
    assert not (config.root_dir / "board_build/host").exists()


@pytest.mark.parametrize("field,value", [("control_header", None), ("ip_directory", "../escape"),
                                        ("control_header", "/tmp/outside"), ("tool_version", "2024.1"),
                                        ("vlnv", "wrong:hls:ip:1.0"), ("schema_version", 2)])
def test_bad_manifest_rejected(exported, field, value):
    config, path = exported
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError): control_spec_from_export(config, path, intent("scalar_probe")[0])


@pytest.mark.parametrize("change", ["part", "clock"])
def test_wrong_current_target_rejected(exported, change):
    config, path = exported
    if change == "clock":
        config = resolved_config(config.root_dir, board=config.board, requested_hz=245.76e6,
                                 hls_target_hz=250e6)
    else:
        config = resolved_config(config.root_dir, board=replace(config.board, part="xc7z020clg484-1"))
    with pytest.raises(ValueError, match="current Board/HLS target"):
        control_spec_from_export(config, path, intent("scalar_probe")[0])


def test_relabeling_manifest_does_not_relabel_actual_report(exported):
    config, path = exported
    other = resolved_config(config.root_dir, board=config.board, requested_hz=245.76e6,
                            hls_target_hz=250e6)
    record = json.loads(path.read_text())
    record["target"] = _board_target(other)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="actual synthesis report"):
        control_spec_from_export(other, path, intent("scalar_probe")[0])


def test_scalar_mismatch_does_not_publish_or_replace_driver(exported):
    config, path = exported
    good = run_steps(config, path)
    before = _hash_tree(config.root_dir / "board_build")
    bad, _ = intent("scalar_probe")
    bad._offsets["y"] = 0x30
    results = run_steps(config, path, bad)
    assert not results["abi"].success
    assert "offset mismatch" in results["abi"].message
    assert _hash_tree(config.root_dir / "board_build") == before
    assert good["driver"].path("host_driver").is_file()


def test_symlinked_evidence_rejected(exported):
    config, path = exported
    original = path.parent / "control.h"
    target = path.parent / "target.h"
    original.rename(target)
    original.symlink_to(target)
    with pytest.raises(ValueError, match="regular artifact"):
        control_spec_from_export(config, path, intent("scalar_probe")[0])


def test_export_changed_during_parse_is_rejected(exported, monkeypatch):
    from waveflow.build import hls_control
    config, path = exported
    original = hls_control.parse_control_abi
    def change_after_parse(*args, **kwargs):
        result = original(*args, **kwargs)
        (path.parent / "ip/extra.txt").write_text("changed")
        return result
    monkeypatch.setattr(hls_control, "parse_control_abi", change_after_parse)
    with pytest.raises(ValueError, match="changed during ABI"):
        control_spec_from_export(config, path, intent("scalar_probe")[0])


@pytest.mark.parametrize("kwargs", [{"spec_artifact": "ip_manifest"}, {"abi_artifact": ""}])
def test_abi_artifact_names_are_validated(kwargs):
    with pytest.raises(ValueError): HlsControlSpecStep(regmap=intent("scalar_probe")[0], **kwargs)


@pytest.mark.parametrize("kwargs", [{"driver_artifact": "hls_control_spec"}, {"manifest_artifact": ""}])
def test_driver_artifact_names_are_validated(kwargs):
    with pytest.raises(ValueError): HostDriverGenStep(**kwargs)
