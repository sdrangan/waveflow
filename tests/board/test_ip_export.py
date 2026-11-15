from __future__ import annotations

import json
import re
import stat
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from waveflow.board import Board, ClockProfile, ClockRequest, ClockResolution
from waveflow.board.clock import _context
from waveflow.build import ip_export as api
from waveflow.build.board_steps import ExportIpStep
from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep
from waveflow.build.ip_export import IpExportSpec, export_ip, synthesize_for_board


SPEC = IpExportSpec(name="probe")
RTL = "module probe(input ap_clk, input [31:0] s_axi_control_WDATA); endmodule\n"


def report_xml(part, period, top="probe", version="2025.1"):
    return f"""<profile><ReportVersion><Version>{version}</Version></ReportVersion>
<UserAssignments><Part>{part}</Part><TopModelName>{top}</TopModelName>
<TargetClockPeriod>{period}</TargetClockPeriod><FlowTarget>vivado</FlowTarget></UserAssignments>
<InterfaceSummary>
<RtlPorts><name>ap_clk</name><Dir>in</Dir><Bits>1</Bits></RtlPorts>
<RtlPorts><name>s_axi_control_WDATA</name><Dir>in</Dir><Bits>32</Bits></RtlPorts>
</InterfaceSummary></profile>"""


def component_xml():
    return """<component xmlns="http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009"
xmlns:s="http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009">
<vendor>waveflow</vendor><library>hls</library><name>probe</name><version>1.0</version>
<model><ports>
<port><name>ap_clk</name><wire><direction>in</direction></wire></port>
<port><name>s_axi_control_WDATA</name><wire><direction>in</direction>
<vector><left>31</left><right>0</right></vector></wire></port>
</ports></model>
<busInterfaces><busInterface><name>s_axi_control</name>
<busType s:vendor="xilinx.com" s:library="interface" s:name="aximm" s:version="1.0"/>
<slave/><portMaps><portMap><logicalPort><name>WDATA</name></logicalPort>
<physicalPort><name>s_axi_control_WDATA</name></physicalPort></portMap></portMaps>
</busInterface></busInterfaces>
<fileSets><fileSet><name>RTL</name><file><name>hdl/verilog/probe.v</name></file></fileSet></fileSets>
</component>"""


@pytest.fixture
def config(tmp_path):
    return BuildConfig(root_dir=tmp_path, board=Board.rfsoc4x2(), clock_profile="ps_control")


@pytest.fixture
def fake_tools(monkeypatch):
    state = {"calls": [], "report": None, "package": None, "failure": None}
    monkeypatch.setattr(api, "find_vitis_path", lambda: "/mock/vitis-run")
    monkeypatch.setattr(api, "tool_version", lambda *args: "2025.1")

    def run(script, work_dir):
        text = script.read_text()
        work_dir = Path(work_dir)
        state["calls"].append(script.name)
        if state["failure"] == script.name:
            raise subprocess.CalledProcessError(1, "vitis", output="tool output", stderr="tool error")
        if script.name == "synthesize.tcl":
            part = re.search(r"set_part \{([^}]+)\}", text)[1]
            period = re.search(r"create_clock -period (\S+)", text)[1]
            top = re.search(r"set_top (\S+)", text)[1]
            solution = work_dir / "hls" / "solution1"
            (solution / "syn/report").mkdir(parents=True)
            (solution / "syn/verilog").mkdir()
            (solution / ".autopilot/db/coregen").mkdir(parents=True)
            report = report_xml(part, f"{float(period):.2f}", top)
            if state["report"]:
                report = state["report"](report)
            (solution / "syn/report/csynth.xml").write_text(report)
            (solution / f"syn/verilog/{top}.v").write_text(RTL)
            (solution / ".autopilot/db/coregen/control.h").write_text("#define ADDR_X_DATA 0x10\n")
            (work_dir / "hls/hls.app").write_text("project")
            sentinel = "WAVEFLOW_BOARD_CSYNTH_OK"
        else:
            files = {"component.xml": component_xml(), "hdl/verilog/probe.v": RTL}
            if state["package"]:
                state["package"](files)
            with zipfile.ZipFile(work_dir / "ip.zip", "w") as archive:
                for name, contents in files.items():
                    archive.writestr(name, contents)
            sentinel = "WAVEFLOW_IP_EXPORT_OK"
        return subprocess.CompletedProcess(["vitis"], 0, sentinel + "\n", "")

    monkeypatch.setattr(api, "run_vitis_hls", run)
    return state


def synth(config):
    source = config.root_dir / "probe.cpp"
    source.write_text("unsigned probe(unsigned x) { return x + 1; }\n")
    return synthesize_for_board(config, top="probe", inputs={"probe.cpp": source}, sources=("probe.cpp",))


def test_board_synth_and_export_keep_original_solution(config, fake_tools):
    receipt = synth(config)
    before = api._hash_tree(receipt.parent)
    result = export_ip(config, receipt, SPEC)
    manifest = json.loads(result.manifest.read_text())
    assert manifest["target"]["part"] == "xczu48dr-ffvg1517-2-e"
    assert manifest["target"]["hls_period_ns"] == 10
    assert manifest["target"]["clock"]["kind"] == "declared"
    assert manifest["vlnv"] == "waveflow:hls:probe:1.0"
    assert manifest["ports"]["s_axi_control_WDATA"]["width"] == 32
    assert manifest["interfaces"]["s_axi_control"]["bus_type"]["name"] == "aximm"
    assert manifest["control_header_sha256"] == api._sha(result.manifest.parent / "control.h")
    assert manifest["synthesized_verilog_hashes"]["probe.v"] == api._sha(result.directory / "hdl/verilog/probe.v")
    assert api._hash_tree(receipt.parent) == before
    assert fake_tools["calls"] == ["synthesize.tcl", "export.tcl"]


def test_unique_runs_preserve_prior_packages(config, fake_tools):
    receipt = synth(config)
    first = export_ip(config, receipt, SPEC)
    old = api._hash_tree(first.manifest.parent)
    second = export_ip(config, receipt, SPEC)
    assert first.directory != second.directory
    assert api._hash_tree(first.manifest.parent) == old


@pytest.mark.parametrize("mutation", [
    lambda s: s.replace("xczu48dr-ffvg1517-2-e", "xc7z020clg484-1"),
    lambda s: s.replace("<TargetClockPeriod>10.00", "<TargetClockPeriod>4.00"),
    lambda s: s.replace("<TopModelName>probe", "<TopModelName>other"),
    lambda s: s.replace("<Version>2025.1", "<Version>2024.2"),
    lambda s: s.replace("<FlowTarget>vivado", "<FlowTarget>vitis"),
    lambda s: "invalid XML",
])
def test_bad_synthesis_report_never_creates_receipt(config, fake_tools, mutation):
    fake_tools["report"] = mutation
    with pytest.raises(ValueError):
        synth(config)
    assert not list(config.root_dir.rglob("synthesis_receipt.json"))
    assert fake_tools["calls"] == ["synthesize.tcl"]


@pytest.mark.parametrize("relative", [
    "inputs/probe.cpp", "synthesize.tcl", "hls/solution1/syn/report/csynth.xml",
    "hls/solution1/syn/verilog/probe.v", "hls/solution1/.autopilot/db/coregen/control.h",
])
def test_changed_receipt_inputs_refuse_export(config, fake_tools, relative):
    receipt = synth(config)
    path = receipt.parent / relative
    path.write_text(path.read_text() + "\nchanged\n")
    with pytest.raises(ValueError, match="stale synthesis receipt"):
        export_ip(config, receipt, SPEC)
    assert fake_tools["calls"] == ["synthesize.tcl"]


def test_changed_board_or_hls_target_refuses_export(config, fake_tools):
    receipt = synth(config)
    changed = BuildConfig(root_dir=config.root_dir, board=config.board, clock_profile="rf_axis")
    with pytest.raises(ValueError, match="current Board/HLS target"):
        export_ip(changed, receipt, SPEC)
    wrong = BuildConfig(root_dir=config.root_dir,
                        board=replace(config.board, part="xc7z020clg484-1"), clock_profile="ps_control")
    with pytest.raises(ValueError, match="current Board/HLS target"):
        export_ip(wrong, receipt, SPEC)
    assert fake_tools["calls"] == ["synthesize.tcl"]


@pytest.mark.parametrize("mutation", [
    lambda f: f.update({"component.xml": f["component.xml"].replace("<version>1.0", "<version>2.0")}),
    lambda f: f.update({"component.xml": f["component.xml"].replace("<left>31", "<left>63")}),
    lambda f: f.update({"component.xml": f["component.xml"].replace("<name>s_axi_control</name>", "<name>other</name>")}),
    lambda f: f.pop("hdl/verilog/probe.v"),
    lambda f: f.update({"hdl/verilog/probe.v": "// wrong RTL"}),
    lambda f: f.update({"component.xml": "<broken>"}),
    lambda f: f.update({"component.xml": f["component.xml"].replace('s:name="aximm"', '')}),
    lambda f: f.update({"component.xml": f["component.xml"].replace("<slave/>", "")}),
    lambda f: f.update({"component.xml": f["component.xml"].replace("<slave/>", "<slave/><master/>")}),
    lambda f: f.update({"../escaped.txt": "unsafe"}),
    lambda f: f.update({"nested/component.xml": f["component.xml"]}),
])
def test_bad_package_is_not_published(config, fake_tools, mutation):
    receipt = synth(config)
    first = export_ip(config, receipt, SPEC)
    old = api._hash_tree(first.manifest.parent)
    fake_tools["package"] = mutation
    with pytest.raises(RuntimeError, match="invalid IP export"):
        export_ip(config, receipt, SPEC)
    assert api._hash_tree(first.manifest.parent) == old
    assert list(config.root_dir.rglob("ip_manifest.json")) == [first.manifest]


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_missing_control_header_refuses_package(config, fake_tools, kind):
    receipt = synth(config)
    path = receipt.parent / "hls/solution1/.autopilot/db/coregen/control.h"
    if kind == "missing":
        path.unlink()
    else:
        path.write_text("")
    # Model missing evidence in the synthesis output, rather than a hash mismatch.
    record = json.loads(receipt.read_text())
    record["project_hashes"] = api._hash_tree(receipt.parent / "hls")
    receipt.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="requires HLS control.h"):
        export_ip(config, receipt, SPEC)
    assert fake_tools["calls"] == ["synthesize.tcl", "export.tcl"]


def test_tool_failure_retains_log_and_preserves_source(config, fake_tools):
    receipt = synth(config)
    before = api._hash_tree(receipt.parent)
    fake_tools["failure"] = "export.tcl"
    with pytest.raises(RuntimeError, match="inspect"):
        export_ip(config, receipt, SPEC)
    assert api._hash_tree(receipt.parent) == before
    assert not list(config.root_dir.rglob("ip_manifest.json"))
    assert "tool error" in next(config.root_dir.rglob("export.log")).read_text()


def test_version_mismatch_does_not_invoke_hls(config, fake_tools, monkeypatch):
    monkeypatch.setattr(api, "tool_version", lambda *args: "2024.2")
    with pytest.raises(RuntimeError, match="required"):
        synth(config)
    assert not fake_tools["calls"]


def test_explicit_synthesis_and_export_sentinels(config, fake_tools, monkeypatch):
    original = api.run_vitis_hls

    def echoed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.stdout = '# puts "' + result.stdout.strip() + '"\n'
        return result

    monkeypatch.setattr(api, "run_vitis_hls", echoed)
    with pytest.raises(RuntimeError, match="successful completion"):
        synth(config)
    assert not list(config.root_dir.rglob("synthesis_receipt.json"))


def test_physical_readback_is_not_the_hls_target(config, fake_tools):
    request = ClockRequest("ps_pl0", 100e6, 100e6)
    resolution = ClockResolution(
        request=request, profile=ClockProfile(99_999_985, 100e6),
        board_fingerprint=_context(config.board, request.source)[1],
        generator_parameters={"divisor0": 15}, work_dir=config.root_dir, resolved_at="test",
    )
    config = BuildConfig(root_dir=config.root_dir, board=config.board, resolved_clock=resolution)
    result = export_ip(config, synth(config), SPEC)
    target = json.loads(result.manifest.read_text())["target"]
    assert target["hls_period_ns"] == 10
    assert target["clock"]["profile"]["physical_hz"] == 99_999_985


def test_report_rounding_uses_hls_report_precision():
    assert api._period_matches("4.07", 1e9 / 245.76e6)
    assert not api._period_matches("4.00", 1e9 / 245.76e6)
    assert not api._period_matches("nan", 10)
    assert not api._period_matches(None, 10)


@pytest.mark.parametrize("kwargs", [
    {"name": "bad; puts exploit"}, {"vendor": "a:b"}, {"version": "latest"},
    {"library": "../lib"}, {"required_interfaces": "s_axi_control"},
])
def test_invalid_ip_identity(kwargs):
    with pytest.raises(ValueError):
        IpExportSpec(**{"name": "probe", **kwargs})


def test_rendered_tcl_is_export_only_and_names_are_pinned():
    text = api.render_export_tcl(SPEC)
    assert "export_design -format ip_catalog" in text
    assert "config_rtl -deadlock_detection none" in text
    assert "-vendor {waveflow} -library {hls} -ipname {probe} -version {1.0}" in text
    assert "-flow" not in text and "csynth_design" not in text and "-reset" not in text


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a\\b", "a/../../b"])
def test_unsafe_input_paths(config, fake_tools, name):
    with pytest.raises(ValueError, match="unsafe"):
        synthesize_for_board(config, top="probe", inputs={name: Path(__file__)}, sources=(name,))
    assert not fake_tools["calls"]


def test_zip_symlink_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        member = zipfile.ZipInfo("component.xml")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(member, "/etc/passwd")
    with pytest.raises(ValueError, match="unsafe"):
        api._unpack(archive, tmp_path / "out")


def test_dag_explicit_export_and_configurable_artifact_names(config, fake_tools):
    receipt = synth(config)
    dag = BuildDag()
    dag.add(SourceStep(artifact="receipt", path=receipt))
    dag.add(ExportIpStep(name="export", spec=SPEC, receipt_artifact="receipt",
                        directory_artifact="directory", archive_artifact="zip",
                        manifest_artifact="manifest"))
    first = dag.run(config, through="export", force=["export"])
    second = dag.run(config, through="export", force=["export"])
    assert first["export"].success and second["export"].success
    assert not first["export"].skipped and not second["export"].skipped
    assert first["export"].path("manifest") != second["export"].path("manifest")
    assert fake_tools["calls"] == ["synthesize.tcl", "export.tcl", "export.tcl"]
    # A changed snapshot is not blessed even when all receipt/file mtimes look fresh.
    (receipt.parent / "inputs/probe.cpp").write_text("changed")
    rejected = dag.run(config, force=["export"])
    assert not rejected["export"].success
    assert "stale synthesis receipt" in rejected["export"].message


def test_dag_downstream_demand_uses_existing_contract(config, fake_tools):
    class Consumer(BuildStep):
        consumes = ["ip_manifest"]
        produces = {"checked": Path("checked.txt")}

        def run(self, config, ip_manifest):
            output = config.root_dir / "checked.txt"
            output.write_text(str(ip_manifest))
            return {"checked": output}

    receipt = synth(config)
    dag = BuildDag()
    dag.add(SourceStep(artifact="hls_synthesis_receipt", path=receipt))
    dag.add(ExportIpStep(name="export", spec=SPEC))
    # In-memory-only steps require demand or an explicit force, unchanged BuildDag semantics.
    assert dag.run(config)["export"].skipped
    dag.add(Consumer(name="consumer"))
    results = dag.run(config)
    assert results["export"].success and not results["export"].skipped
    assert results["consumer"].success
    assert dag.run(config)["consumer"].skipped


def test_normal_dag_steps_still_cache_files(tmp_path):
    class Normal(BuildStep):
        produces = {"out": Path("out.txt")}

        def run(self, config, **kwargs):
            path = config.root_dir / "out.txt"
            path.write_text("output")
            return {"out": path}

    dag = BuildDag()
    dag.add(Normal(name="normal"))
    config = BuildConfig(root_dir=tmp_path)
    assert dag.run(config)["normal"].success
    assert dag.run(config)["normal"].skipped



def test_export_cannot_copy_a_project_into_itself(config, fake_tools):
    receipt = synth(config)
    before = api._hash_tree(receipt.parent)
    with pytest.raises(ValueError, match="outside the source"):
        export_ip(config, receipt, SPEC, work_dir=receipt.parent / "hls/exports")
    assert api._hash_tree(receipt.parent) == before
    assert fake_tools["calls"] == ["synthesize.tcl"]


def test_duplicate_step_paths_or_artifact_names_fail():
    with pytest.raises(ValueError, match="distinct"):
        ExportIpStep(spec=SPEC, directory_artifact="same", archive_artifact="same")


def test_control_less_ip_records_absence(config, fake_tools):
    receipt = synth(config)
    (receipt.parent / "hls/solution1/.autopilot/db/coregen/control.h").unlink()
    record = json.loads(receipt.read_text())
    record["project_hashes"] = api._hash_tree(receipt.parent / "hls")
    receipt.write_text(json.dumps(record))

    def no_control(files):
        files["component.xml"] = re.sub(
            r"<busInterfaces>.*?</busInterfaces>", "", files["component.xml"], flags=re.S)
    fake_tools["package"] = no_control
    result = export_ip(config, receipt, IpExportSpec(name="probe", required_interfaces=()))
    manifest = json.loads(result.manifest.read_text())
    assert manifest["control_header"] is None
    assert manifest["control_header_sha256"] is None
    assert manifest["interfaces"] == {}
    assert not (result.manifest.parent / "control.h").exists()


def test_empty_required_interfaces_cannot_bypass_control_evidence(config, fake_tools):
    receipt = synth(config)
    (receipt.parent / "hls/solution1/.autopilot/db/coregen/control.h").unlink()
    record = json.loads(receipt.read_text())
    record["project_hashes"] = api._hash_tree(receipt.parent / "hls")
    receipt.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="requires HLS control.h"):
        export_ip(config, receipt, IpExportSpec(name="probe", required_interfaces=()))
    assert not list(config.root_dir.rglob("ip_manifest.json"))


@pytest.mark.parametrize("kind", ["missing", "invalid", "unsupported"])
def test_invalid_receipt_refuses_tool_call(config, fake_tools, kind):
    path = config.root_dir / "receipt.json"
    if kind == "invalid":
        path.write_text("{")
    elif kind == "unsupported":
        path.write_text('{"schema_version": 100}')
    with pytest.raises(ValueError, match="receipt"):
        export_ip(config, path, SPEC)
    assert fake_tools["calls"] == []


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_missing_or_corrupt_export_archive(config, fake_tools, monkeypatch, kind):
    receipt = synth(config)
    original = api.run_vitis_hls

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        path = Path(kwargs["work_dir"]) / "ip.zip"
        if kind == "missing":
            path.unlink()
        else:
            path.write_text("not a zip")
        return result

    monkeypatch.setattr(api, "run_vitis_hls", run)
    with pytest.raises(RuntimeError, match="invalid IP export"):
        export_ip(config, receipt, SPEC)
    assert not list(config.root_dir.rglob("ip_manifest.json"))


def test_config_is_required_and_rechecked(config, fake_tools):
    with pytest.raises(ValueError, match="requires a Board"):
        synth(BuildConfig(root_dir=config.root_dir))
    receipt = synth(config)
    config.clk_freq = 250e6
    with pytest.raises(ValueError, match="no longer matches"):
        export_ip(config, receipt, SPEC)
    assert fake_tools["calls"] == ["synthesize.tcl"]


def test_export_tool_version_is_rechecked(config, fake_tools, monkeypatch):
    receipt = synth(config)
    monkeypatch.setattr(api, "tool_version", lambda *args: "2024.2")
    with pytest.raises(RuntimeError, match="required"):
        export_ip(config, receipt, SPEC)
    assert fake_tools["calls"] == ["synthesize.tcl"]


@pytest.mark.parametrize("failure", ["missing_sentinel", "error_sentinel", "nonzero"])
def test_export_success_must_be_explicit(config, fake_tools, monkeypatch, failure):
    receipt = synth(config)
    original = api.run_vitis_hls

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        if failure == "missing_sentinel":
            result.stdout = ""
        elif failure == "error_sentinel":
            result.stdout += "  WAVEFLOW_ERROR: failure\n"
        else:
            result.returncode = 1
        return result

    monkeypatch.setattr(api, "run_vitis_hls", run)
    with pytest.raises(RuntimeError, match="successful completion"):
        export_ip(config, receipt, SPEC)
    assert not list(config.root_dir.rglob("ip_manifest.json"))


def test_source_receipt_is_rechecked_after_tool_run(config, fake_tools, monkeypatch):
    receipt = synth(config)
    original = api.run_vitis_hls

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        (receipt.parent / "inputs/probe.cpp").write_text("concurrent change")
        return result

    monkeypatch.setattr(api, "run_vitis_hls", run)
    with pytest.raises(RuntimeError, match="stale synthesis receipt"):
        export_ip(config, receipt, SPEC)
    assert not list(config.root_dir.rglob("ip_manifest.json"))


def test_fractional_hls_target_remains_separate_from_rf_physical_clock(config, fake_tools):
    # The named RF profile targets 250 MHz HLS, not its 245.76 MHz physical rate.
    config = BuildConfig(root_dir=config.root_dir, board=config.board, clock_profile="rf_axis")
    manifest = json.loads(export_ip(config, synth(config), SPEC).manifest.read_text())
    assert manifest["target"]["hls_period_ns"] == 4
    assert manifest["target"]["clock"]["profile"]["physical_hz"] == 245.76e6
    assert not api._period_matches("4", 1e9 / 245.76e6)
