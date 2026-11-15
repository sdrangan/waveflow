"""Template-driven, isolated Vivado block-design and overlay builds.

One exported HLS IP is staged with a verified ABI. The committed template owns
topology; explicit contracts own connections, clocks and addresses. No port-to-BD
inference, shared DAG cache changes, or physical-board execution lives here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from waveflow.build.build import BuildConfig
from waveflow.build.hls_control import HlsControlSpec
from waveflow.build.ip_export import (
    _board_target, _hash_tree, _identifier, _inside, _new_run, _period_matches,
    _sha, _write_json,
)
from waveflow.toolchain import run_vivado


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _bd_path(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_/]*", value),
             "invalid BD object path")
    return value


def _word(value):
    value = str(value)
    _require(not any(c in value for c in "{}\\\n\r"), "unsafe Tcl parameter")
    return "{" + value + "}"


@dataclass(frozen=True)
class BdAddress:
    """Explicit byte aperture, not an HLS register offset."""
    space: str
    segment: str
    offset: int
    size: int

    def __post_init__(self):
        _bd_path(self.space)
        _bd_path(self.segment)
        _require(type(self.offset) is int and type(self.size) is int
                 and self.offset >= 0 and self.size >= 4096
                 and self.size & (self.size - 1) == 0
                 and self.offset % self.size == 0 and self.offset + self.size <= 2**64,
                 "address must be an aligned power-of-two aperture of at least 4 KiB")


@dataclass(frozen=True)
class BdConnection:
    kind: str  # net or interface
    pins: tuple[str, ...]

    def __post_init__(self):
        _require(self.kind in ("net", "interface") and len(self.pins) >= 2
                 and len(set(self.pins)) == len(self.pins), "invalid connection contract")
        for pin in self.pins:
            _bd_path(pin)


@dataclass(frozen=True)
class BdProperty:
    kind: str  # cell or pin
    path: str
    name: str
    value: str

    def __post_init__(self):
        _require(self.kind in ("cell", "pin"), "invalid property object kind")
        _bd_path(self.path)
        _require(re.fullmatch(r"[A-Za-z0-9_.]+", self.name), "invalid property name")
        _word(self.value)


@dataclass(frozen=True)
class BdDesign:
    """A committed template plus independently declared structural expectations.

Template variables: WF_BD, WF_VLNV, WF_IP_CELL, WF_REQUEST_HZ. The framework
creates the project/BD, registers repositories, and assigns the declared map.
The template must only create/connect cells, never launch synthesis. Additional
non-HLS IPs (e.g. SmartConnect) belong to the template and property contracts.
"""
    name: str
    template: Path
    ip_cell: str
    addresses: tuple[BdAddress, ...]
    clock_pin: str
    connections: tuple[BdConnection, ...]
    properties: tuple[BdProperty, ...] = ()
    clock_source: str = "ps_pl0"
    clock_tolerance_ppm: float = 1.0

    def __post_init__(self):
        _identifier(self.name, "BD name")
        _bd_path(self.ip_cell)
        _bd_path(self.clock_pin)
        object.__setattr__(self, "template", Path(self.template))
        for name in ("addresses", "connections", "properties"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        _require(self.addresses and self.connections, "address and connection contracts are required")
        _require(isinstance(self.clock_tolerance_ppm, (float, int))
                 and not isinstance(self.clock_tolerance_ppm, bool)
                 and math.isfinite(self.clock_tolerance_ppm)
                 and 0 <= self.clock_tolerance_ppm <= 1000, "invalid clock tolerance")
        _require(self.clock_source in ("ps_pl0", "pl_ref_mmcm"), "unsupported board clock recipe")
        for index, address in enumerate(self.addresses):
            _require(isinstance(address, BdAddress), "expected BdAddress")
            for other in self.addresses[:index]:
                _require((address.space, address.segment) != (other.space, other.segment),
                         "duplicate address segment in address space")
                _require(address.space != other.space or
                         max(address.offset, other.offset) >= min(address.offset + address.size,
                                                                  other.offset + other.size),
                         "overlapping address map")
        _require(all(isinstance(c, BdConnection) for c in self.connections), "expected BdConnection")
        _require(all(isinstance(p, BdProperty) for p in self.properties), "expected BdProperty")

    def record(self):
        return {**asdict(self), "template": "sources/template.tcl"}


def _export_evidence(config, path, abi):
    """Recheck S0b evidence and the S0c binding before staging any Vivado input."""
    _require(isinstance(abi, HlsControlSpec), "a verified HlsControlSpec is required")
    digest = _sha(path)
    root = path.parent
    r = json.loads(path.read_text())
    _require(r["schema_version"] == 1 and r["validation_level"] == "hls_ip_export",
             "not an S0b export manifest")
    _require(r["target"] == _board_target(config) and r["tool_version"] == abi.tool_version,
             "export target differs from current Board/HLS target")
    package = _inside(root, r["ip_directory"])
    _require(_hash_tree(package) == r["package_hashes"], "stale IP package")
    evidence = {"ip_manifest.json": path}
    for rel, expected in ((r["archive"], r["archive_sha256"]),
                          (r["csynth_report"], r["csynth_report_sha256"]),
                          ("synthesis_receipt.json", r["synthesis_receipt_sha256"]),
                          ("synthesize.tcl", r["synthesis_tcl_sha256"]),
                          ("export.tcl", r["export_tcl_sha256"]),
                          (r["control_header"], r["control_header_sha256"])):
        source = _inside(root, rel)
        _require(_sha(source) == expected, f"stale export evidence: {rel}")
        evidence[rel] = source
    _require(abi.control_header_sha256 == r["control_header_sha256"]
             and abi.component_xml_sha256 == r["component_xml_sha256"]
             and _sha(package / "component.xml") == abi.component_xml_sha256
             and abi.vlnv == r["vlnv"], "ABI does not match exported IP")
    report = ET.parse(_inside(root, r["csynth_report"])).getroot()
    a = report.find("UserAssignments")
    target = r["target"]
    _require(a is not None and a.findtext("Part", "").replace("-", "").lower()
             == target["part"].replace("-", "").lower()
             and a.findtext("TopModelName") == r["top"]
             and a.findtext("FlowTarget") == "vivado"
             and _period_matches(a.findtext("TargetClockPeriod"), target["hls_period_ns"])
             and report.findtext("ReportVersion/Version") == r["tool_version"],
             "actual synthesis report differs from target")
    receipt = json.loads((root / "synthesis_receipt.json").read_text())
    _require(receipt["target"] == target and receipt["top"] == r["top"]
             and receipt["input_hashes"] == r["input_hashes"], "receipt/export mismatch")
    _require(digest == _sha(path), "export manifest changed during validation")
    return r, package, evidence


def render_bd_tcl(target, design, abi):
    """Render the project wrapper; topology remains in the committed template."""
    d = design.record()
    clock = target["clock"]
    physical = clock["profile"]["physical_hz"]
    requested = clock.get("request", {}).get("requested_hz", physical)
    lines = [
        "# S0d project-relative build. Never reset or overwrite an existing project.",
        "set WF_ROOT [file dirname [file normalize [info script]]]",
        "cd $WF_ROOT",
        f"set WF_BD {_word(d['name'])}", f"set WF_VLNV {_word(abi.vlnv)}",
        f"set WF_IP_CELL {_word(d['ip_cell'])}", f"set WF_REQUEST_HZ {requested:.17g}",
        'if {[file exists project]} {error "refusing to overwrite existing project"}',
        'set_param general.maxThreads 2',
        'set_param board.repoPaths [list [file normalize sources/boards]]',
        f"if {{[llength [get_board_parts -quiet {_word(target['board_part'])}]] != 1}} {{error \"board part unavailable\"}}",
        f"create_project board_build project -part {_word(target['part'])}",
        f"set_property board_part {_word(target['board_part'])} [current_project]",
        'set_property ip_repo_paths [list [file normalize sources/ip]] [current_project]',
        'update_ip_catalog',
        'if {[llength [get_ipdefs -all $WF_VLNV]] != 1} {error "IP VLNV unavailable or ambiguous"}',
        'create_bd_design $WF_BD', 'source sources/template.tcl',
        '# Explicit address assignments; do not import the reference application map.',
    ]
    for space in sorted({a["space"] for a in d["addresses"]}):
        lines.append(f"if {{[llength [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces {_word(space)}]]] != 0}} "
                     '{error "template must not preassign the declared address space"}')
    for address in d["addresses"]:
        lines.append(f"assign_bd_address -offset 0x{address['offset']:X} -range 0x{address['size']:X} "
                     f"-target_address_space [get_bd_addr_spaces {_word(address['space'])}] "
                     f"[get_bd_addr_segs {_word(address['segment'])}]")
    lines += ['validate_bd_design', 'save_bd_design',
              f"wf_equal [get_property PART [current_project]] {_word(target['part'])} part",
              f"wf_equal [get_property BOARD_PART [current_project]] {_word(target['board_part'])} board_part",
              'wf_equal [get_property VLNV [get_bd_cells $WF_IP_CELL]] $WF_VLNV ip_vlnv']
    for p in d["properties"]:
        getter = "get_bd_cells" if p["kind"] == "cell" else "get_bd_pins"
        lines.append(f"wf_equal [get_property {_word(p['name'])} [{getter} {_word(p['path'])}]] "
                     f"{_word(p['value'])} {_word(p['path'] + ':' + p['name'])}")
    for conn in d["connections"]:
        pins = " ".join(_word(p) for p in conn["pins"])
        lines.append(f"wf_connected {conn['kind']} [list {pins}]")
    lines += [f"set actual_hz [get_property CONFIG.FREQ_HZ [get_bd_pins {_word(d['clock_pin'])}]]",
              f"if {{![string is double -strict $actual_hz] || $actual_hz <= 0 || "
              f"abs(double($actual_hz) - {physical:.17g}) / {physical:.17g} * 1e6 > {d['clock_tolerance_ppm']:.17g}}} "
              '{error "physical clock differs from requested profile"}',
              'set verdict [open bd_verdict.tsv w]', 'puts $verdict "clock_hz\t$actual_hz"']
    # Enumerate all mapped segments in each declared space. Equality rejects extra/unexpected windows.
    for space in sorted({a["space"] for a in d["addresses"]}):
        lines += [f"foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces {_word(space)}]] {{",
                  '    set target_seg [get_bd_addr_segs -of_objects $seg]',
                  f'    puts $verdict "address\\t{space}\\t$target_seg\\t[get_property OFFSET $seg]\\t[get_property RANGE $seg]"',
                  '}']
    lines += ['close $verdict',
              'foreach run [get_runs] {wf_equal [get_property PROGRESS $run] 0% no_synthesis}',
              'write_bd_tcl -include_layout validated_bd.tcl', 'save_bd_design']
    helpers = '''proc wf_equal {actual expected label} {
    if {$actual ne $expected} {error "$label mismatch: $actual != $expected"}
}
proc wf_connected {kind pins} {
    set getter get_bd_pins
    set nets get_bd_nets
    if {$kind eq "interface"} {set getter get_bd_intf_pins; set nets get_bd_intf_nets}
    set first ""
    foreach pin $pins {
        set obj [$getter -quiet $pin]
        if {[llength $obj] != 1} {error "missing or ambiguous pin: $pin"}
        set net [$nets -quiet -of_objects $obj]
        if {[llength $net] != 1} {error "unconnected pin: $pin"}
        if {$first eq ""} {set first $net} else {wf_equal $net $first "connection $pin"}
    }
}
'''
    return helpers + "\n".join(lines) + "\n"


def _entry(body, sentinel):
    return ('if {[catch {\n' + body + '\n} message options]} {\n'
            '    puts stderr "WAVEFLOW_ERROR: $message"\n'
            '    puts stderr [dict get $options -errorinfo]\n    exit 1\n}\n'
            f'puts "{sentinel}"\nexit 0\n')


def prepare_bd(config, manifest_path, abi, design, *, work_dir):
    """Snapshot relocatable board/IP/template inputs; do not launch Vivado."""
    _require(isinstance(design, BdDesign), "expected BdDesign")
    manifest_path = Path(manifest_path).resolve()
    record, package, evidence = _export_evidence(config, manifest_path, abi)
    target = record["target"]
    config.board.clock_source(design.clock_source)
    clock = target["clock"]
    if clock["kind"] == "resolved":
        _require(clock["request"]["source"] == design.clock_source, "BD clock recipe mismatch")
    else:
        _require((clock["name"], design.clock_source) in
                 (("ps_control", "ps_pl0"), ("rf_axis", "pl_ref_mmcm")),
                 "declared profile has no supported BD clock recipe")
    matches = [a for a in design.addresses
               if a.segment.startswith(design.ip_cell + "/" + abi.interface + "/")]
    _require(len(matches) == 1 and matches[0].size >= abi.address_range,
             "address map does not cover the verified control ABI")
    template_hash = _sha(design.template)
    script = render_bd_tcl(target, design, abi)
    run = _new_run(Path(work_dir), "bd-inputs-")
    sources = run / "sources"
    sources.mkdir()
    shutil.copytree(package, sources / "ip")
    shutil.copytree(config.board.board_files_dir,
                    sources / "boards" / config.board.name / config.board.board_files_version)
    for name, source in evidence.items():
        dest = _inside(sources / "evidence", name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    shutil.copyfile(design.template, sources / "template.tcl")
    _write_json(sources / "control_abi.json", {**abi.to_dict(), "abi_sha256": abi.abi_sha256})
    (run / "create_bd.tcl").write_text(script)
    (run / "validate.tcl").write_text(_entry("source create_bd.tcl", "WAVEFLOW_BD_OK"))
    (run / "bitstream.tcl").write_text(_entry("source create_bd.tcl\n" + _BITSTREAM_TCL,
                                              "WAVEFLOW_BITSTREAM_OK"))
    _export_evidence(config, manifest_path, abi)
    for name, original in evidence.items():
        _require(_sha(_inside(sources / "evidence", name)) == _sha(original),
                 "export evidence changed while staging")
    _require(_hash_tree(sources / "ip") == record["package_hashes"]
             and _sha(sources / "template.tcl") == template_hash == _sha(design.template)
             and _hash_tree(sources / "boards" / config.board.name / config.board.board_files_version)
             == target["board_file_hashes"], "inputs changed while staging BD")
    # A commit identifies the source revision, while input hashes also describe dirty/untracked work.
    revision = subprocess.run(["git", "-C", str(design.template.resolve().parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
    provenance = revision.stdout.strip() if revision.returncode == 0 else None
    manifest = run / "bd_inputs.json"
    _write_json(manifest, {
        "schema_version": 1, "validation_level": "bd_inputs", "target": target,
        "design": design.record(), "abi_sha256": abi.abi_sha256,
        "ip_manifest_sha256": _sha(manifest_path), "source_commit": provenance,
        "files": _hash_tree(run),
    })
    return manifest


def _read_inputs(config, path):
    record = json.loads(path.read_text())
    _require(record["schema_version"] == 1 and record["validation_level"] == "bd_inputs",
             "not a BD input manifest")
    _require(record["target"] == _board_target(config), "BD target differs from current config")
    _require(record["files"], "empty BD input manifest")
    for name, digest in record["files"].items():
        _require(_sha(_inside(path.parent, name)) == digest, f"stale BD input: {name}")
    required = {"create_bd.tcl", "validate.tcl", "bitstream.tcl", "sources/template.tcl",
                "sources/control_abi.json", "sources/ip/component.xml"}
    _require(required <= record["files"].keys(), "missing required BD inputs")
    expected_sources = {n[len("sources/"):]: h for n, h in record["files"].items() if n.startswith("sources/")}
    _require(_hash_tree(path.parent / "sources") == expected_sources, "unlisted or changed BD source")
    return record


def _clone_inputs(config, path, parent, prefix):
    before = _sha(path)
    record = _read_inputs(config, path)
    run = _new_run(Path(parent), prefix)
    for name in record["files"]:
        dest = _inside(run, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_inside(path.parent, name), dest)
    shutil.copyfile(path, run / "bd_inputs.json")
    _read_inputs(config, run / "bd_inputs.json")
    _require(before == _sha(path), "BD inputs changed during copy")
    return record, run


def _run(config, run, script, sentinel):
    try:
        result = run_vivado(run / script, run, required_version=config.board.vivado_version)
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        (run / "tool_output.log").write_text(
            str(exc) + "\n" + (getattr(exc, "stdout", "") or "") + "\n"
            + (getattr(exc, "stderr", "") or ""))
        raise RuntimeError(f"Vivado failed; inspect {run / 'tool_output.log'}") from exc
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    (run / "tool_output.log").write_text(output)
    _require(result.returncode == 0 and sentinel in output.splitlines()
             and not any(l.lstrip().startswith("WAVEFLOW_ERROR:") for l in output.splitlines()),
             "Vivado did not report successful completion")


def _bd_verdict(run, record):
    rows = [line.split("\t") for line in (run / "bd_verdict.tsv").read_text().splitlines()]
    _require(rows and rows[0][0] == "clock_hz" and len(rows[0]) == 2, "missing clock readback")
    actual = float(rows[0][1])
    expected = record["target"]["clock"]["profile"]["physical_hz"]
    _require(math.isfinite(actual) and actual > 0 and
             abs(actual - expected) / expected * 1e6 <= record["design"]["clock_tolerance_ppm"],
             "physical clock readback outside tolerance")
    addresses = []
    for row in rows[1:]:
        _require(len(row) == 5 and row[0] == "address", "invalid address readback")
        addresses.append({"space": row[1].lstrip("/"), "segment": row[2].lstrip("/"),
                          "offset": int(row[3], 0), "size": int(row[4], 0)})
    key = lambda a: (a["space"], a["segment"], a["offset"], a["size"])
    _require(sorted(addresses, key=key) == sorted(record["design"]["addresses"], key=key),
             "Vivado address map differs from declared map")
    return {"physical_hz": actual, "addresses": addresses, "structural_checks": "passed"}


def validate_bd(config, inputs_path, *, work_dir):
    """Rung 4: a fresh project, structural checks, NO synthesis."""
    inputs_path = Path(inputs_path).resolve()
    record, run = _clone_inputs(config, inputs_path, work_dir, "bd-validate-")
    _run(config, run, "validate.tcl", "WAVEFLOW_BD_OK")
    _read_inputs(config, run / "bd_inputs.json")
    verdict = _bd_verdict(run, record)
    path = run / "bd_validation.json"
    _write_json(path, {
        "schema_version": 1, "validation_level": "bd_validated", "target": record["target"],
        "inputs_sha256": _sha(run / "bd_inputs.json"), "verdict": verdict,
        "files": {n: _sha(run / n) for n in ("bd_verdict.tsv", "validated_bd.tcl", "tool_output.log")},
        "synthesis_performed": False,
    })
    return path


def _read_receipt(config, path, level):
    record = json.loads(path.read_text())
    _require(record["schema_version"] == 1 and record["validation_level"] == level,
             f"expected {level} receipt")
    inputs = path.parent / "bd_inputs.json"
    _require(_sha(inputs) == record["inputs_sha256"], "stale BD input manifest")
    source = _read_inputs(config, inputs)
    _require(record["target"] == source["target"], "receipt target mismatch")
    required = {"bd_verdict.tsv", "validated_bd.tcl", "tool_output.log"}
    if level == "bd_validated":
        _require(record.get("synthesis_performed") is False, "invalid structural-only receipt")
    else:
        required |= {"artifacts/design.bit", "artifacts/design.hwh", "artifacts/design.xsa",
                     "reports/timing.rpt", "reports/check_timing.rpt", "reports/drc.rpt",
                     "reports/route.rpt", "reports/metrics.tsv", "prior_bd_validation.json"}
    _require(required <= record["files"].keys(), "receipt is missing required output hashes")
    for name, digest in record["files"].items():
        _require(_sha(_inside(path.parent, name)) == digest, f"stale Vivado output: {name}")
    _require(record["verdict"] == _bd_verdict(path.parent, source), "stale BD verdict")
    return record, source


_BITSTREAM_TCL = r'''
set bd_file [get_files */${WF_BD}.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
set_property top ${WF_BD}_wrapper [current_fileset]
update_compile_order -fileset sources_1
launch_runs synth_1 -jobs 2
wait_on_run synth_1
wf_equal [get_property PROGRESS [get_runs synth_1]] 100% synthesis
launch_runs impl_1 -to_step write_bitstream -jobs 2
wait_on_run impl_1
wf_equal [get_property PROGRESS [get_runs impl_1]] 100% implementation
open_run impl_1
file mkdir reports
report_timing_summary -report_unconstrained -file reports/timing.rpt
check_timing -verbose -file reports/check_timing.rpt
report_drc -file reports/drc.rpt
report_route_status -file reports/route.rpt
set setup_path [get_timing_paths -delay_type max -max_paths 1]
set hold_path [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup_path] != 1 || [llength $hold_path] != 1} {error "missing timing paths"}
set wns [get_property SLACK $setup_path]
set whs [get_property SLACK $hold_path]
set severe 0
foreach violation [get_drc_violations -quiet] {
    if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {incr severe}
}
set fp [open reports/metrics.tsv w]
puts $fp "wns_ns\t$wns"
puts $fp "whs_ns\t$whs"
puts $fp "severe_drc\t$severe"
close $fp
if {$wns < 0 || $whs < 0 || $severe != 0} {error "timing or DRC gate failed"}
file mkdir artifacts
file copy project/board_build.runs/impl_1/${WF_BD}_wrapper.bit artifacts/design.bit
set hwh project/board_build.gen/sources_1/bd/${WF_BD}/hw_handoff/${WF_BD}.hwh
if {![file isfile $hwh]} {
    set hwh project/board_build.srcs/sources_1/bd/${WF_BD}/hw_handoff/${WF_BD}.hwh
}
file copy $hwh artifacts/design.hwh
write_hw_platform -fixed -include_bit -file artifacts/design.xsa
'''


def _implementation_verdict(run, source):
    pairs = [line.split("\t") for line in (run / "reports/metrics.tsv").read_text().splitlines()]
    _require(len(pairs) == 3 and all(len(p) == 2 for p in pairs), "invalid timing metrics")
    values = dict(pairs)
    _require(set(values) == {"wns_ns", "whs_ns", "severe_drc"}, "missing implementation metrics")
    wns, whs = float(values["wns_ns"]), float(values["whs_ns"])
    _require(math.isfinite(wns) and math.isfinite(whs) and wns >= 0 and whs >= 0
             and int(values["severe_drc"]) == 0, "timing or DRC gate failed")
    report = (run / "reports/timing.rpt").read_text()
    _require("All user specified timing constraints are met." in report, "timing summary did not pass")
    check = (run / "reports/check_timing.rpt").read_text()
    for section in ("no_clock", "unconstrained_internal_endpoints", "no_input_delay", "no_output_delay"):
        counts = re.findall(r"checking " + section + r"\s*\((\d+)\)", check, re.IGNORECASE)
        _require(counts and all(c == "0" for c in counts),
                 f"unconstrained or missing timing check: {section}")
    for ext in ("bit", "hwh", "xsa"):
        p = run / f"artifacts/design.{ext}"
        _require(p.is_file() and not p.is_symlink() and p.stat().st_size > 0, f"missing {ext} artifact")
    # HWH is independently checked against the declared AXI aperture.
    root = ET.parse(run / "artifacts/design.hwh").getroot()
    expected = source["design"]
    cell = expected["ip_cell"]
    regions = root.findall(".//MEMRANGE")
    for address in expected["addresses"]:
        if not address["segment"].startswith(cell + "/"):
            continue
        matches = [r for r in regions if r.get("INSTANCE") == cell
                   and r.get("SLAVEBUSINTERFACE") == address["segment"].split("/")[1]]
        _require(matches and all(int(r.get("BASEVALUE"), 0) == address["offset"]
                                and int(r.get("HIGHVALUE"), 0) == address["offset"] + address["size"] - 1
                                for r in matches), "HWH address map differs from BD")
    with zipfile.ZipFile(run / "artifacts/design.xsa") as z:
        bits = [n for n in z.namelist() if n.endswith(".bit")]
        _require(len(bits) == 1 and hashlib.sha256(z.read(bits[0])).hexdigest()
                 == _sha(run / "artifacts/design.bit"), "XSA does not contain the matching bitstream")
    return {"wns_ns": wns, "whs_ns": whs, "severe_drc": 0,
            "timing_passed": True, "drc_passed": True}


def build_bitstream(config, validation_path, *, work_dir):
    """Rung 5 requires a valid rung-4 receipt and repeats validation in a fresh project."""
    validation_path = Path(validation_path).resolve()
    receipt_hash = _sha(validation_path)
    _read_receipt(config, validation_path, "bd_validated")
    source, run = _clone_inputs(config, validation_path.parent / "bd_inputs.json", work_dir, "bitstream-")
    _run(config, run, "bitstream.tcl", "WAVEFLOW_BITSTREAM_OK")
    _read_inputs(config, run / "bd_inputs.json")
    verdict = _bd_verdict(run, source)
    implementation = _implementation_verdict(run, source)
    _read_receipt(config, validation_path, "bd_validated")
    _require(_sha(validation_path) == receipt_hash, "validation receipt changed during implementation")
    shutil.copyfile(validation_path, run / "prior_bd_validation.json")
    files = {p.relative_to(run).as_posix(): _sha(p)
             for directory in ("artifacts", "reports") for p in sorted((run / directory).rglob("*"))
             if p.is_file()}
    files.update({n: _sha(run / n) for n in ("bd_verdict.tsv", "validated_bd.tcl", "tool_output.log",
                                           "prior_bd_validation.json")})
    path = run / "bitstream_manifest.json"
    _write_json(path, {
        "schema_version": 1, "validation_level": "implemented_bitstream", "target": source["target"],
        "inputs_sha256": _sha(run / "bd_inputs.json"), "verdict": verdict,
        "implementation": implementation, "files": files, "validation_receipt_sha256": receipt_hash,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return path


def collect_overlay(config, bitstream_path, driver_manifest, *, work_dir):
    """Collect only receipt-bound outputs; never glob a Vivado project for 'latest'."""
    bitstream_path, driver_manifest = Path(bitstream_path).resolve(), Path(driver_manifest).resolve()
    before = (_sha(bitstream_path), _sha(driver_manifest))
    result, source = _read_receipt(config, bitstream_path, "implemented_bitstream")
    _require(result["implementation"] == _implementation_verdict(bitstream_path.parent, source),
             "implementation verdict mismatch")
    driver = json.loads(driver_manifest.read_text())
    _require(driver["schema_version"] == 1 and driver["validation_level"] == "generated_control_driver"
             and driver["abi_sha256"] == source["abi_sha256"], "driver ABI differs from bitstream ABI")
    driver_path = _inside(driver_manifest.parent, driver["driver"])
    abi_path = driver_manifest.parent / "control_abi.json"
    _require(_sha(driver_path) == driver["driver_sha256"]
             and _sha(abi_path) == driver["control_abi_sha256"], "stale host driver")
    _require(json.loads(abi_path.read_text()) == json.loads(
        (bitstream_path.parent / "sources/control_abi.json").read_text()), "driver ABI content mismatch")
    run = _new_run(Path(work_dir), "overlay-")
    name = source["design"]["name"]
    for ext in ("bit", "hwh", "xsa"):
        shutil.copyfile(bitstream_path.parent / f"artifacts/design.{ext}", run / f"{name}.{ext}")
    shutil.copytree(bitstream_path.parent / "reports", run / "reports")
    shutil.copyfile(driver_path, run / "driver.py")
    shutil.copyfile(abi_path, run / "control_abi.json")
    shutil.copyfile(driver_manifest, run / "driver_manifest.json")
    # Carry a self-contained source bundle so relocation does not need the old workspace.
    _, replay = _clone_inputs(config, bitstream_path.parent / "bd_inputs.json", run, "rebuild-")
    _read_receipt(config, bitstream_path, "implemented_bitstream")
    _require(before == (_sha(bitstream_path), _sha(driver_manifest)), "inputs changed during collection")
    for ext in ("bit", "hwh", "xsa"):
        _require(_sha(run / f"{name}.{ext}") == result["files"][f"artifacts/design.{ext}"],
                 "artifact changed during collection")
    _require(_sha(run / "driver.py") == driver["driver_sha256"]
             and _sha(run / "control_abi.json") == driver["control_abi_sha256"]
             and _sha(run / "driver_manifest.json") == before[1],
             "driver changed during collection")
    for report in (run / "reports").rglob("*"):
        if report.is_file():
            _require(_sha(report) == result["files"][report.relative_to(run).as_posix()],
                     "report changed during collection")
    manifest = run / "manifest.json"
    _write_json(manifest, {
        "schema_version": 1, "validation_level": "collected_overlay", "target": source["target"],
        "source_commit": source["source_commit"], "ip_manifest_sha256": source["ip_manifest_sha256"],
        "abi_sha256": source["abi_sha256"], "bitstream_manifest_sha256": before[0],
        "driver_manifest_sha256": before[1], "address_map": result["verdict"]["addresses"],
        "physical_hz": result["verdict"]["physical_hz"], "implementation": result["implementation"],
        "rebuild_inputs": str(replay.relative_to(run) / "bd_inputs.json"),
        "files": _hash_tree(run), "board_execution_verified": False,
    })
    return manifest
