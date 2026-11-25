"""Tool-free S0d contracts. Tool outputs below are explicitly synthetic test doubles."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from waveflow.build import vivado_board as vb
from waveflow.build.board_steps import BdGenStep, BdValidateStep, BitstreamStep, CollectOverlayStep
from waveflow.build.hls_control import control_spec_from_export
from waveflow.build.host_driver import generate_host_driver
from waveflow.build.ip_export import _sha, _hash_tree, _write_json
from tests.board.test_control_steps import exported as exported
from tests.board.control_abi_helpers import intent
from tests.board.vivado_helpers import minimal_design


@pytest.fixture
def staged(exported):
    config, export = exported
    abi = control_spec_from_export(config, export, intent("scalar_probe")[0])
    path = vb.prepare_bd(config, export, abi, minimal_design(), work_dir=config.root_dir / "bd")
    return config, path, abi, export


def fake_vivado(script, work_dir, **kwargs):
    """Emit deterministic fake tool files; never presented as a real Vivado gate."""
    root = Path(work_dir)
    source = json.loads((root / "bd_inputs.json").read_text())
    assert kwargs["required_version"] == "2025.1"
    lines = ["clock_hz\t99999985"]
    for a in source["design"]["addresses"]:
        lines.append(f"address\t{a['space']}\t{a['segment']}\t{a['offset']:#x}\t{a['size']:#x}")
    (root / "bd_verdict.tsv").write_text("\n".join(lines) + "\n")
    (root / "validated_bd.tcl").write_text("# fake validated BD, no hardware evidence\n")
    sentinel = "WAVEFLOW_BD_OK"
    if script.name == "bitstream.tcl":
        (root / "reports").mkdir()
        (root / "artifacts").mkdir()
        (root / "reports/metrics.tsv").write_text("wns_ns\t1.5\nwhs_ns\t0.1\nsevere_drc\t0\n")
        (root / "reports/timing.rpt").write_text("All user specified timing constraints are met.\n")
        (root / "reports/check_timing.rpt").write_text("\n".join(
            f"Checking {s} (0)" for s in ("no_clock", "unconstrained_internal_endpoints", "no_input_delay", "no_output_delay")))
        (root / "reports/drc.rpt").write_text("synthetic DRC report\n")
        (root / "reports/route.rpt").write_text("synthetic routing report\n")
        (root / "artifacts/design.bit").write_bytes(b"synthetic bitstream, not programmable")
        a = source["design"]["addresses"][0]
        (root / "artifacts/design.hwh").write_text(
            f'<SYSTEM><MEMRANGE INSTANCE="probe" SLAVEBUSINTERFACE="s_axi_control" '
            f'BASEVALUE="{a["offset"]:#x}" HIGHVALUE="{a["offset"] + a["size"] - 1:#x}"/></SYSTEM>')
        with zipfile.ZipFile(root / "artifacts/design.xsa", "w") as z:
            z.write(root / "artifacts/design.bit", "s0d_control_wrapper.bit")
        sentinel = "WAVEFLOW_BITSTREAM_OK"
    return subprocess.CompletedProcess([], 0, sentinel + "\n", "")


@pytest.fixture
def mock_tools(monkeypatch):
    monkeypatch.setattr(vb, "run_vivado", fake_vivado)


@pytest.fixture
def validated(staged, mock_tools):
    config, path, abi, export = staged
    validation = vb.validate_bd(config, path, work_dir=config.root_dir / "validation")
    return config, validation, abi


@pytest.fixture
def implemented(validated):
    config, validation, abi = validated
    bit = vb.build_bitstream(config, validation, work_dir=config.root_dir / "implementation")
    driver = generate_host_driver(abi, config.root_dir / "driver")
    return config, bit, driver.manifest


def test_staging_is_relocatable_and_preserves_inputs(staged):
    config, path, abi, export = staged
    before = _hash_tree(export.parent)
    record = vb._read_inputs(config, path)
    assert record["abi_sha256"] == abi.abi_sha256
    assert record["target"]["part"] == "xczu48dr-ffvg1517-2-e"
    script = (path.parent / "create_bd.tcl").read_text()
    assert str(config.root_dir) not in script
    assert "sources/boards" in script and "sources/ip" in script
    assert "refusing to overwrite" in script
    assert "reset_project" not in script and "create_project -force" not in script
    assert "launch_runs" not in script and "synth_design" not in script
    assert "validate_bd_design" in script and "get_bd_addr_segs" in script
    assert script == (Path(__file__).parent / "fixtures/vivado/create_bd.golden.tcl").read_text()
    assert "wf_connected interface" in script and "CONFIG.POLARITY" in script
    assert _hash_tree(export.parent) == before
    second = vb.prepare_bd(config, export, abi, minimal_design(), work_dir=config.root_dir / "bd")
    assert second != path
    assert (second.parent / "create_bd.tcl").read_bytes() == (path.parent / "create_bd.tcl").read_bytes()


@pytest.mark.parametrize("field,value", [("name", "bad;exec"), ("ip_cell", "../escape"),
    ("clock_pin", "ps/pl_clk0]"), ("clock_tolerance_ppm", -1), ("clock_tolerance_ppm", float("nan")),
    ("clock_tolerance_ppm", True), ("clock_tolerance_ppm", 1e6), ("clock_source", "unknown"),
    ("connections", ()), ("addresses", ())])
def test_invalid_design_rejected(field, value):
    with pytest.raises((ValueError, TypeError)):
        replace(minimal_design(), **{field: value})


@pytest.mark.parametrize("offset,size", [(1, 4096), (-4096, 4096), (0, 0), (0, 3072),
    (0, 512), (2**64, 4096), (True, 4096), (0, True)])
def test_bad_address_rejected(offset, size):
    with pytest.raises(ValueError): vb.BdAddress("ps/Data", "probe/s_axi_control/Reg", offset, size)


def test_overlapping_and_duplicate_maps_rejected():
    design = minimal_design()
    for other in (design.addresses[0], vb.BdAddress("ps/Data", "another/s_axi_control/Reg", 0xA1000000, 4096)):
        with pytest.raises(ValueError): replace(design, addresses=(*design.addresses, other))


def test_ddr_target_can_be_mapped_by_two_distinct_dma_masters():
    design = minimal_design()
    addresses = tuple(vb.BdAddress(f"dma/Data_{channel}", "ps/SAXIGP2/HP0_DDR_LOW", 0, 2**31)
                      for channel in ("MM2S", "S2MM"))
    actual = replace(design, addresses=(*design.addresses, *addresses))
    assert len(actual.addresses) == 3
    with pytest.raises(ValueError, match="duplicate address segment"):
        replace(design, addresses=(*addresses, addresses[0]))


@pytest.mark.parametrize("factory", [
    lambda: vb.BdConnection("wrong", ("a/b", "c/d")),
    lambda: vb.BdConnection("net", ("a/b", "a/b")),
    lambda: vb.BdProperty("cell", "a", "CONFIG.X]", "1"),
    lambda: vb.BdProperty("pin", "a/b", "CONFIG.X", "} ; exit"),
])
def test_bad_contract_values(factory):
    with pytest.raises(ValueError): factory()


@pytest.mark.parametrize("file", ["sources/template.tcl", "sources/ip/component.xml",
    "sources/control_abi.json", "create_bd.tcl", "validate.tcl", "bitstream.tcl"])
def test_stale_inputs_fail_before_vivado(staged, monkeypatch, file):
    config, path, _, _ = staged
    (path.parent / file).write_text("changed")
    monkeypatch.setattr(vb, "run_vivado", lambda *a, **k: pytest.fail("must not call tool"))
    with pytest.raises(ValueError, match="stale BD input"):
        vb.validate_bd(config, path, work_dir=config.root_dir / "reject")
    assert not (config.root_dir / "reject").exists()


def test_unlisted_source_rejected(staged):
    config, path, _, _ = staged
    (path.parent / "sources/ip/extra.v").write_text("extra")
    with pytest.raises(ValueError, match="unlisted"):
        vb._read_inputs(config, path)


def test_symlink_source_rejected(staged):
    config, path, _, _ = staged
    file = path.parent / "sources/template.tcl"
    target = path.parent / "other.tcl"
    file.rename(target)
    file.symlink_to(target)
    with pytest.raises(ValueError): vb._read_inputs(config, path)


def test_wrong_abi_and_aperture_rejected(staged):
    config, _, abi, export = staged
    for spec, design in ((replace(abi, vlnv="wrong:hls:ip:1.0"), minimal_design()),
                         (replace(abi, address_range=2**24), minimal_design()),
                         (abi, replace(minimal_design(), clock_source="pl_ref_mmcm"))):
        with pytest.raises(ValueError):
            vb.prepare_bd(config, export, spec, design, work_dir=config.root_dir / "reject")
    assert not (config.root_dir / "reject").exists()


@pytest.mark.parametrize("mode", ["exception", "missing_sentinel", "error_sentinel", "nonzero"])
def test_tool_failure_publishes_no_receipt(staged, monkeypatch, mode):
    config, path, _, _ = staged
    def tool(*a, **kw):
        if mode == "exception": raise subprocess.CalledProcessError(1, "vivado", output="license error")
        return subprocess.CompletedProcess([], 1 if mode == "nonzero" else 0,
            "WAVEFLOW_BD_OK\n" if mode != "missing_sentinel" else "", "WAVEFLOW_ERROR: wrong" if mode == "error_sentinel" else "")
    monkeypatch.setattr(vb, "run_vivado", tool)
    with pytest.raises((RuntimeError, ValueError)):
        vb.validate_bd(config, path, work_dir=config.root_dir / "reject")
    assert not list((config.root_dir / "reject").rglob("bd_validation.json"))


@pytest.mark.parametrize("mode", ["address", "clock", "extra", "nan", "missing"])
def test_readback_mismatch_rejected(staged, monkeypatch, mode):
    config, path, _, _ = staged
    def tool(script, root, **kw):
        result = fake_vivado(script, root, **kw)
        verdict = root / "bd_verdict.tsv"
        text = verdict.read_text()
        if mode == "address": text = text.replace("0xa1000000", "0xa2000000")
        if mode == "clock": text = text.replace("99999985", "200000000")
        if mode == "nan": text = text.replace("99999985", "nan")
        if mode == "extra": text += "address\tps/Data\tother/Reg\t0xb0000000\t0x10000\n"
        if mode == "missing": text = ""
        verdict.write_text(text)
        return result
    monkeypatch.setattr(vb, "run_vivado", tool)
    with pytest.raises(ValueError): vb.validate_bd(config, path, work_dir=config.root_dir / "reject")


def test_validation_preserves_input_bundle_and_is_relocatable(staged, mock_tools):
    config, path, _, _ = staged
    before = _hash_tree(path.parent)
    moved = config.root_dir / "relocated with spaces"
    shutil.copytree(path.parent, moved)
    result = vb.validate_bd(config, moved / "bd_inputs.json", work_dir=config.root_dir / "validated")
    receipt = json.loads(result.read_text())
    assert receipt["synthesis_performed"] is False
    assert receipt["verdict"]["physical_hz"] == 99999985
    assert _hash_tree(path.parent) == before


def test_bitstream_requires_prior_bd_receipt(staged, monkeypatch):
    config, path, _, _ = staged
    monkeypatch.setattr(vb, "run_vivado", lambda *a, **k: pytest.fail("must not call tool"))
    with pytest.raises(ValueError, match="bd_validated"):
        vb.build_bitstream(config, path, work_dir=config.root_dir / "reject")


@pytest.mark.parametrize("fault", ["setup", "hold", "drc", "unconstrained", "missing_check", "summary", "hwh", "xsa", "missing_bit"])
def test_implementation_failures_do_not_publish(validated, monkeypatch, fault):
    config, validation, _ = validated
    def tool(script, root, **kw):
        result = fake_vivado(script, root, **kw)
        if fault in ("setup", "hold", "drc"):
            file = root / "reports/metrics.tsv"
            old, new = {"setup": ("1.5", "-1.5"), "hold": ("0.1", "-0.1"), "drc": ("severe_drc\t0", "severe_drc\t1")}[fault]
            file.write_text(file.read_text().replace(old, new))
        elif fault in ("unconstrained", "missing_check"):
            file = root / "reports/check_timing.rpt"
            file.write_text(file.read_text().replace("no_clock (0)", "no_clock (1)" if fault == "unconstrained" else "unknown (0)"))
        elif fault == "summary": (root / "reports/timing.rpt").write_text("Timing constraints are NOT met.")
        elif fault == "hwh":
            file = root / "artifacts/design.hwh"
            file.write_text(file.read_text().replace("0xa1000000", "0xa2000000"))
        elif fault == "xsa":
            with zipfile.ZipFile(root / "artifacts/design.xsa", "w") as z: z.writestr("wrong.bit", b"wrong")
        else: (root / "artifacts/design.bit").unlink()
        return result
    monkeypatch.setattr(vb, "run_vivado", tool)
    with pytest.raises(ValueError):
        vb.build_bitstream(config, validation, work_dir=config.root_dir / "reject")
    assert not list((config.root_dir / "reject").rglob("bitstream_manifest.json"))


def test_overlay_has_four_files_and_keeps_build_evidence(implemented):
    config, bit, driver = implemented
    before = (_hash_tree(bit.parent), _hash_tree(driver.parent))
    path = vb.collect_overlay(config, bit, driver, work_dir=config.root_dir / "overlay")
    record = json.loads(path.read_text())
    assert record["schema_version"] == 2
    assert not record["board_execution_verified"]
    assert record["address_map"][0]["offset"] == 0xA1000000
    assert record["physical_hz"] == 99999985
    for name, digest in record["files"].items(): assert _sha(path.parent / name) == digest
    assert set(record["files"]) == {"s0d_control.bit", "s0d_control.hwh", "driver.py"}
    assert {p.name for p in path.parent.iterdir()} == {*record["files"], "manifest.json"}
    assert record["vlnv"] == json.loads(driver.read_text())["vlnv"]
    for ext in ("bit", "hwh"):
        assert _sha(path.parent / ("s0d_control." + ext)) == _sha(bit.parent / ("artifacts/design." + ext))
    assert (bit.parent / "artifacts/design.xsa").is_file()
    assert (bit.parent / "reports/timing.rpt").is_file()
    vb._read_inputs(config, bit.parent / "bd_inputs.json")
    assert (_hash_tree(bit.parent), _hash_tree(driver.parent)) == before


@pytest.mark.parametrize("file", ["artifacts/design.bit", "artifacts/design.hwh", "artifacts/design.xsa",
    "reports/timing.rpt", "bd_verdict.tsv", "validated_bd.tcl", "sources/template.tcl"])
def test_stale_implementation_cannot_be_collected(implemented, file):
    config, bit, driver = implemented
    (bit.parent / file).write_text("changed")
    with pytest.raises(ValueError): vb.collect_overlay(config, bit, driver, work_dir=config.root_dir / "reject")
    assert not (config.root_dir / "reject").exists()


@pytest.mark.parametrize("fault", ["abi", "driver", "manifest"])
def test_wrong_driver_rejected(implemented, fault):
    config, bit, driver = implemented
    if fault == "driver": (driver.parent / "driver.py").write_text("wrong")
    elif fault == "abi": (driver.parent / "control_abi.json").write_text("{}")
    else:
        r = json.loads(driver.read_text()); r["abi_sha256"] = "wrong"; _write_json(driver, r)
    with pytest.raises(ValueError): vb.collect_overlay(config, bit, driver, work_dir=config.root_dir / "reject")


@pytest.mark.parametrize("factory", [
    lambda: BdGenStep(design=minimal_design(), inputs_artifact="ip_manifest"),
    lambda: BdValidateStep(validation_artifact="bd_inputs"),
    lambda: BitstreamStep(bitstream_artifact=""),
    lambda: CollectOverlayStep(overlay_artifact="host_driver_manifest"),
])
def test_bad_artifact_names(factory):
    with pytest.raises(ValueError): factory()


def test_all_four_steps_use_existing_dag_contract(staged, mock_tools):
    from waveflow.build.build import BuildDag, SourceStep
    from waveflow.build.board_steps import HlsControlSpecStep, HostDriverGenStep
    config, _, _, export = staged
    dag = BuildDag()
    dag.add(SourceStep(artifact="ip_manifest", path=export))
    dag.add(HlsControlSpecStep(name="abi", regmap=intent("scalar_probe")[0]))
    dag.add(HostDriverGenStep(name="driver"))
    dag.add(BdGenStep(name="bd", design=minimal_design()))
    dag.add(BdValidateStep(name="validate"))
    dag.add(BitstreamStep(name="bit"))
    dag.add(CollectOverlayStep(name="overlay"))
    names = ["abi", "driver", "bd", "validate", "bit", "overlay"]
    results = dag.run(config, through="overlay", force=names)
    for name in names:
        assert results[name].success, results[name].traceback
        assert not results[name].skipped


@pytest.mark.parametrize("fault", ["missing_hashes", "synthesis", "target", "stale_log"])
def test_bad_validation_receipt_rejected(validated, monkeypatch, fault):
    config, path, _ = validated
    r = json.loads(path.read_text())
    if fault == "missing_hashes": r["files"] = {}
    elif fault == "synthesis": r["synthesis_performed"] = True
    elif fault == "target": r["target"] = {}
    else: (path.parent / "tool_output.log").write_text("changed")
    _write_json(path, r)
    monkeypatch.setattr(vb, "run_vivado", lambda *a, **k: pytest.fail("must not call tool"))
    with pytest.raises(ValueError):
        vb.build_bitstream(config, path, work_dir=config.root_dir / "reject")


def test_corrupt_copy_is_not_published(implemented, monkeypatch):
    config, bit, driver = implemented
    original = vb.shutil.copyfile
    def corrupt(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        if Path(dst).name == "s0d_control.bit":
            Path(dst).write_bytes(b"broken copy")
        return result
    monkeypatch.setattr(vb.shutil, "copyfile", corrupt)
    with pytest.raises(ValueError, match="artifact changed during collection"):
        vb.collect_overlay(config, bit, driver, work_dir=config.root_dir / "reject")
    assert not list((config.root_dir / "reject").rglob("manifest.json"))


def test_real_report_table_of_contents_repeats_zero_counts(implemented):
    config, bit, _ = implemented
    report = bit.parent / "reports/check_timing.rpt"
    report.write_text(report.read_text() + "\n" + report.read_text())
    source = vb._read_inputs(config, bit.parent / "bd_inputs.json")
    assert vb._implementation_verdict(bit.parent, source)["timing_passed"]
    report.write_text(report.read_text() + "\nChecking no_clock (1)\n")
    with pytest.raises(ValueError, match="no_clock"):
        vb._implementation_verdict(bit.parent, source)


def test_tcl_clock_guard_uses_fractional_division(staged):
    _, path, _, _ = staged
    guard = next(line for line in (path.parent / "create_bd.tcl").read_text().splitlines()
                 if line.startswith("if ") and "physical clock differs" in line)
    # Tcl integer division would truncate sub-100% errors to zero. Real Vivado
    # rejection tests assert this exact guard fails, not only the Python readback.
    assert "abs(double($actual_hz) -" in guard
