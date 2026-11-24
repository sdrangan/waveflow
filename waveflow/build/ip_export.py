"""Board-targeted HLS synthesis and isolated IP-catalog export.

A receipt binds a fresh solution to its snapshotted inputs and Board/HLS target.
Export rechecks that receipt, works on a copy, and publishes only a verified package.
This is not full-BD validation, implementation timing closure, or hardware validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping

from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import tcl_target
from waveflow.toolchain import find_vitis_path, run_vitis_hls, tool_version
from waveflow.utils.csynthparse import synth_target


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{label} must be a simple identifier")
    return value


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value) or str(path) == "."):
        raise ValueError(f"unsafe relative artifact path: {value!r}")
    return path.as_posix()


def _inside(root: Path, value: str) -> Path:
    path = root / _relative(value)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"artifact escapes its workspace: {value}")
    return path


def _sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular artifact file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"expected an artifact directory: {root}")
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError(f"symlinks are not supported in build artifacts: {root}")
    return {p.relative_to(root).as_posix(): _sha(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


def _write_json(path: Path, record: dict) -> None:
    path.write_text(json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _board_target(config: BuildConfig) -> dict:
    board = config.board
    if board is None:
        raise ValueError("board-targeted synthesis/export requires a Board")
    if config.resolved_clock is None:
        raise ValueError("board clock request is unresolved; resolve it before packaging hardware")
    config.resolved_clock.require_compatible(board)
    if (config.clock_request is not None
            and config.clock_request != config.resolved_clock.request):
        raise ValueError("clock_request differs from resolved_clock request")
    profile = config.resolved_clock.profile
    source = board.clock_source(config.resolved_clock.request.source)
    clock = {
        "kind": "resolved", "request": asdict(config.resolved_clock.request),
        "reference_clock": source.reference_clock,
        "reference_hz": board.reference_clock(source.reference_clock).frequency_hz,
        "board_fingerprint": config.resolved_clock.board_fingerprint,
        "generator_parameters": dict(config.resolved_clock.generator_parameters),
        "validation_level": config.resolved_clock.validation_level,
    }
    if (config.part != board.part or profile.hls_target_hz is None
            or config.clk_freq != profile.hls_target_hz):
        raise ValueError("BuildConfig no longer matches its board/HLS clock target")
    if config.vitis_version not in (None, board.vivado_version):
        raise ValueError("Vitis version must match the pinned board tool release")
    clock["profile"] = asdict(profile)
    return {
        "board": board.name, "part": board.part, "board_part": board.board_part,
        "board_files_version": board.board_files_version,
        "board_file_hashes": _hash_tree(board.board_files_dir),
        "tool_version": board.vivado_version, "clock": clock,
        "hls_target_hz": config.clk_freq, "hls_period_ns": tcl_target(config)[1],
    }


def _vitis_version(expected: str) -> str:
    executable = find_vitis_path()
    if executable is None:
        raise RuntimeError("Vitis HLS not found; set WAVEFLOW_VITIS_PATH")
    actual = tool_version(executable, "--version")
    if actual != expected:
        raise RuntimeError(f"Vitis release {actual!r} does not match required {expected!r}")
    return actual


def _run(script: Path, workspace: Path, sentinel: str, log: Path) -> None:
    try:
        result = run_vitis_hls(script, work_dir=workspace)
    except subprocess.CalledProcessError as exc:
        log.write_text((exc.stdout or "") + "\n" + (exc.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"Vitis failed; inspect {log}") from exc
    except (OSError, RuntimeError) as exc:
        log.write_text(str(exc) + "\n", encoding="utf-8")
        raise RuntimeError(f"Vitis failed; inspect {log}: {exc}") from exc
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    log.write_text(output, encoding="utf-8")
    if (result.returncode != 0 or sentinel not in output.splitlines()
            or any(line.lstrip().startswith("WAVEFLOW_ERROR:") for line in output.splitlines())):
        raise RuntimeError(f"Vitis did not report successful completion; inspect {log}")


def _new_run(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()


def _period_matches(reported: str, expected: float) -> bool:
    # csynth.xml rounds TargetClockPeriod (e.g. 4.0690104 ns -> 4.07 ns).
    # Compare at the precision actually serialized, not against physical frequency.
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(reported)
        if not value.is_finite() or value <= 0:
            return False
        half_unit = min(Decimal("0.005"), Decimal(10) ** value.as_tuple().exponent / 2)
        return abs(value - Decimal(str(expected))) <= half_unit
    except (InvalidOperation, TypeError, ValueError):
        return False


def _check_report(solution: Path, target: dict, top: str) -> dict:
    report_dir = solution / "syn" / "report"
    report = report_dir / "csynth.xml"
    try:
        xml = ET.parse(report).getroot()
        actual = synth_target(report_dir)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"missing or invalid synthesis report: {report}") from exc
    if actual is None or not actual["part"]:
        raise ValueError("synthesis report has no target part")
    if actual["part"].replace("-", "").lower() != target["part"].replace("-", "").lower():
        raise ValueError(f"synthesized part {actual['part']!r} differs from Board {target['part']!r}")
    assignments = xml.find("UserAssignments")
    if assignments is None or assignments.findtext("TopModelName") != top:
        raise ValueError("synthesis report top does not match the requested IP")
    if not _period_matches(assignments.findtext("TargetClockPeriod"), target["hls_period_ns"]):
        raise ValueError("synthesized HLS period differs from BuildConfig")
    if xml.findtext("ReportVersion/Version") != target["tool_version"]:
        raise ValueError("synthesis report tool version differs from Board")
    if assignments.findtext("FlowTarget") != "vivado":
        raise ValueError("IP-catalog export requires a Vivado-flow HLS solution")
    if not (solution / "syn" / "verilog" / f"{top}.v").is_file():
        raise ValueError("synthesis output is missing the top-level Verilog")
    return actual


def render_board_synth_tcl(top: str, sources: tuple[str, ...], part: str, period_ns: float) -> str:
    _identifier(top, "top")
    if not re.fullmatch(r"[A-Za-z0-9-]+", part):
        raise ValueError("invalid FPGA part")
    if not math.isfinite(period_ns) or period_ns <= 0:
        raise ValueError("invalid HLS period")
    additions = "\n".join(
        f"    add_files {{inputs/{_relative(source)}}} -cflags {{-Iinputs -Iinputs/include}}"
        for source in sources
    )
    return f"""# Fresh Vivado-flow HLS synthesis. Physical clock generation belongs to the board BD.
if {{[catch {{
    open_project hls
    set_top {top}
{additions}
    open_solution solution1 -flow_target vivado
    set_part {{{part}}}
    create_clock -period {period_ns:.17g}
    csynth_design
}} message]}} {{
    puts stderr "WAVEFLOW_ERROR: board synthesis: $message"
    exit 1
}}
puts "WAVEFLOW_BOARD_CSYNTH_OK"
exit 0
"""


def synthesize_for_board(
    config: BuildConfig, *, top: str, inputs: Mapping[str, Path],
    sources: tuple[str, ...], work_dir: str | Path | None = None,
) -> Path:
    """Create a fresh input snapshot + HLS project and return its receipt path.

    'inputs' names every non-toolchain source/header at a relative snapshot path.
    'sources' selects the C/C++ translation units from those paths. All non-toolchain
    includes must resolve within that snapshot (no external/absolute includes).
    No existing project is reset, and no mtime-only solution cache is trusted.

    This deliberately small helper creates evidence at synthesis time; it does
    not certify arbitrary pre-existing solutions. The receipt refers to the
    immutable input snapshot, not live source paths. Changed source inputs need
    a new synthesis receipt. It does not alter any example's synthesis DAG.
    """
    parent = Path(work_dir) if work_dir is not None else config.root_dir / "board_build" / "synthesis"
    if config.board is not None and config.resolved_clock is None:
        if config.clock_request is None:
            raise ValueError("board synthesis requires an application clock request")
        config.resolved_clock = config.board.resolve_clock(
            config.clock_request, work_dir=parent.parent / "clocks",
        )
    target = _board_target(config)
    _identifier(top, "top")
    mapped = {_relative(name): Path(path).resolve() for name, path in inputs.items()}
    if len(mapped) != len(inputs) or not mapped or not sources or len(set(sources)) != len(sources):
        raise ValueError("inputs/sources must be nonempty and have unique paths")
    if any(_relative(name) not in mapped for name in sources):
        raise ValueError("every source must be present in the input snapshot")
    original_hashes = {name: _sha(path) for name, path in mapped.items()}
    _vitis_version(target["tool_version"])
    run = _new_run(parent, "synth-")
    for name, path in mapped.items():
        dest = run / "inputs" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    if _hash_tree(run / "inputs") != original_hashes:
        raise ValueError("inputs changed while taking the synthesis snapshot")
    script = run / "synthesize.tcl"
    script.write_text(render_board_synth_tcl(top, sources, target["part"], target["hls_period_ns"]),
                      encoding="utf-8")
    _run(script, run, "WAVEFLOW_BOARD_CSYNTH_OK", run / "synthesis.log")
    if _hash_tree(run / "inputs") != original_hashes or _board_target(config) != target:
        raise ValueError("synthesis inputs or board target changed during the run")
    actual = _check_report(run / "hls" / "solution1", target, top)
    receipt = {
        "schema_version": 1, "top": top, "target": target,
        "project": "hls", "solution": "solution1", "sources": list(sources),
        "input_hashes": original_hashes, "project_hashes": _hash_tree(run / "hls"),
        "synthesis_tcl_sha256": _sha(script), "report_target": actual,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    path = run / "synthesis_receipt.json"
    _write_json(path, receipt)
    return path


@dataclass(frozen=True)
class IpExportSpec:
    """Explicit IP identity and expected interfaces; no register ABI interpretation.

    Set required_interfaces=() for a control-less design. Any actual AXI slave
    still requires the HLS control header; an empty tuple cannot bypass that gate.
    """

    name: str
    vendor: str = "waveflow"
    library: str = "hls"
    version: str = "1.0"
    required_interfaces: tuple[str, ...] = ("s_axi_control",)

    def __post_init__(self) -> None:
        _identifier(self.name, "IP name")
        _identifier(self.library, "IP library")
        if not isinstance(self.vendor, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.vendor):
            raise ValueError("invalid IP vendor")
        if not isinstance(self.version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", self.version):
            raise ValueError("IP version must be a dotted numeric version")
        if isinstance(self.required_interfaces, str):
            raise ValueError("required_interfaces must be a sequence of names")
        interfaces = tuple(self.required_interfaces)
        if len(set(interfaces)) != len(interfaces):
            raise ValueError("duplicate required interface")
        for name in interfaces:
            _identifier(name, "required interface")
        object.__setattr__(self, "required_interfaces", interfaces)

    @property
    def vlnv(self) -> str:
        return f"{self.vendor}:{self.library}:{self.name}:{self.version}"


@dataclass(frozen=True)
class IpExportResult:
    directory: Path
    archive: Path
    manifest: Path
    log: Path


def render_export_tcl(spec: IpExportSpec) -> str:
    # A fresh solution from synthesize_for_board has no inherited export flow.
    # Do not add '-flow syn/impl': this stage only packages the existing RTL.
    return f"""# Export only; synthesis/implementation of a complete BD is a later stage.
if {{[catch {{
    open_project hls
    open_solution solution1
    # Keep packaged RTL byte-identical to the synthesis receipt. Vitis otherwise
    # injects export-only simulation deadlock monitors into persistent kernels.
    config_rtl -deadlock_detection none
    export_design -format ip_catalog -vendor {{{spec.vendor}}} -library {{{spec.library}}} -ipname {{{spec.name}}} -version {{{spec.version}}} -output {{ip.zip}}
}} message]}} {{
    puts stderr "WAVEFLOW_ERROR: IP export: $message"
    exit 1
}}
puts "WAVEFLOW_IP_EXPORT_OK"
exit 0
"""


def _read_receipt(config: BuildConfig, path: Path) -> dict:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("missing or invalid synthesis receipt; run a fresh board synthesis") from exc
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("project") != "hls" or receipt.get("solution") != "solution1"):
        raise ValueError("unsupported synthesis receipt")
    if receipt.get("target") != _board_target(config):
        raise ValueError("synthesis receipt does not match the current Board/HLS target")
    _identifier(receipt.get("top"), "receipt top")
    root = path.parent
    if receipt.get("input_hashes") != _hash_tree(root / "inputs"):
        raise ValueError("stale synthesis receipt: input snapshot changed")
    if receipt.get("project_hashes") != _hash_tree(root / "hls"):
        raise ValueError("stale synthesis receipt: HLS project/RTL/report changed")
    if receipt.get("synthesis_tcl_sha256") != _sha(root / "synthesize.tcl"):
        raise ValueError("stale synthesis receipt: synthesis Tcl changed")
    _check_report(root / "hls" / "solution1", receipt["target"], receipt["top"])
    return receipt


def _unpack(archive: Path, dest: Path) -> Path:
    """Extract only regular, relative members; never follow ZIP paths or symlinks."""
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 10000 or sum(m.file_size for m in members) > 512 * 1024 * 1024:
            raise ValueError("IP archive exceeds the supported size")
        seen = set()
        for member in members:
            name = member.filename.rstrip("/")
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                    or ":" in name or path.as_posix() in seen
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError(f"unsafe or duplicate IP archive member: {member.filename}")
            seen.add(path.as_posix())
            target = dest.joinpath(*path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
    components = list(dest.rglob("component.xml"))
    if len(components) != 1:
        raise ValueError("IP archive must contain exactly one component.xml")
    return components[0].parent


def inspect_ip(directory: Path, spec: IpExportSpec, report: Path) -> dict:
    """Read IP-XACT ports/interfaces and check them against the actual HLS report."""
    component = directory / "component.xml"
    root = ET.parse(component).getroot()
    text = lambda node, tag: node.findtext("{*}" + tag)
    vlnv = ":".join(text(root, tag) or "" for tag in ("vendor", "library", "name", "version"))
    if vlnv != spec.vlnv:
        raise ValueError(f"exported VLNV {vlnv!r} differs from {spec.vlnv!r}")
    ports = {}
    for port in root.findall("{*}model/{*}ports/{*}port"):
        name = text(port, "name")
        wire = port.find("{*}wire")
        if not name or name in ports or wire is None:
            raise ValueError("invalid or duplicate IP port")
        vector = wire.find("{*}vector")
        width = (abs(int(text(vector, "left")) - int(text(vector, "right"))) + 1
                 if vector is not None else 1)
        direction = text(wire, "direction")
        if direction not in ("in", "out", "inout"):
            raise ValueError(f"invalid IP port direction: {name}")
        ports[name] = {"direction": direction, "width": width}
    expected = {}
    for port in ET.parse(report).getroot().findall("InterfaceSummary/RtlPorts"):
        name = port.findtext("name")
        if not name or name in expected:
            raise ValueError("invalid or duplicate synthesis-report port")
        expected[name] = {"direction": port.findtext("Dir"), "width": int(port.findtext("Bits"))}
    if not expected or ports != expected:
        raise ValueError("IP ports/widths/directions differ from the synthesized RTL report")
    interfaces = {}
    for interface in root.findall("{*}busInterfaces/{*}busInterface"):
        name = text(interface, "name")
        kind = interface.find("{*}busType")
        if not name or name in interfaces or kind is None:
            raise ValueError("invalid or duplicate IP bus interface")
        mappings = {}
        for mapping in interface.findall("{*}portMaps/{*}portMap"):
            logical = mapping.findtext("{*}logicalPort/{*}name")
            physical = mapping.findtext("{*}physicalPort/{*}name")
            if not logical or logical in mappings or physical not in ports:
                raise ValueError("invalid IP interface-to-port mapping")
            mappings[logical] = physical
        bus_type = {key.rsplit("}", 1)[-1]: value for key, value in kind.attrib.items()}
        modes = [mode for mode in ("master", "slave", "mirroredMaster", "mirroredSlave")
                 if interface.find("{*}" + mode) is not None]
        if (any(not bus_type.get(key) for key in ("vendor", "library", "name", "version"))
                or len(modes) != 1 or not mappings):
            raise ValueError("invalid IP bus type, mode, or empty port mapping")
        interfaces[name] = {"bus_type": bus_type, "mode": modes[0], "port_map": mappings}
    if any(name not in interfaces for name in spec.required_interfaces):
        raise ValueError("exported IP is missing a required bus interface")
    referenced = set()
    for file in root.findall("{*}fileSets/{*}fileSet/{*}file"):
        name = text(file, "name")
        if not name:
            raise ValueError("IP file reference has no name")
        referenced.add(_relative(name))
        _sha(_inside(directory, name))
    if not any(name.endswith(".v") for name in referenced):
        raise ValueError("IP package has no referenced Verilog source")
    return {"vlnv": vlnv, "ports": ports, "interfaces": interfaces,
            "referenced_files": sorted(referenced)}


def export_ip(
    config: BuildConfig, receipt_path: str | Path, spec: IpExportSpec,
    *, work_dir: str | Path | None = None,
) -> IpExportResult:
    """Export a verified receipt into a new directory, preserving the original solution."""
    if not isinstance(spec, IpExportSpec):
        raise TypeError("spec must be an IpExportSpec")
    receipt_path = Path(receipt_path).resolve()
    receipt = _read_receipt(config, receipt_path)
    receipt_hash = _sha(receipt_path)
    version = _vitis_version(receipt["target"]["tool_version"])
    source = receipt_path.parent
    control = source / "hls" / "solution1" / ".autopilot" / "db" / "coregen" / "control.h"
    parent = Path(work_dir) if work_dir is not None else config.root_dir / "board_build" / "exports"
    if parent.resolve().is_relative_to(source):
        raise ValueError("export workspace must be outside the source synthesis workspace")
    run = _new_run(parent, "export-")
    shutil.copytree(source / "hls", run / "hls")
    shutil.copytree(source / "inputs", run / "inputs")
    if (_hash_tree(run / "hls") != receipt["project_hashes"]
            or _hash_tree(run / "inputs") != receipt["input_hashes"]):
        raise ValueError("HLS project changed while taking the export snapshot")
    script = run / "export.tcl"
    script.write_text(render_export_tcl(spec), encoding="utf-8")
    log = run / "export.log"
    _run(script, run, "WAVEFLOW_IP_EXPORT_OK", log)
    archive = run / "ip.zip"
    try:
        directory = _unpack(archive, run / "unpacked")
        inventory = inspect_ip(directory, spec, source / "hls/solution1/syn/report/csynth.xml")
        needs_control = any(
            item["bus_type"].get("name") == "aximm" and item["mode"] == "slave"
            for item in inventory["interfaces"].values()
        )
        if needs_control and (not control.is_file() or control.stat().st_size == 0):
            raise ValueError("AXI slave interface requires HLS control.h evidence")
        rtl_root = source / "hls/solution1/syn/verilog"
        rtl_hashes = {p.relative_to(rtl_root).as_posix(): _sha(p)
                      for p in sorted(rtl_root.rglob("*.v"))}
        for name, digest in rtl_hashes.items():
            packaged_name = "hdl/verilog/" + name
            if (packaged_name not in inventory["referenced_files"]
                    or _sha(_inside(directory, packaged_name)) != digest):
                raise ValueError("packaged Verilog differs from the verified synthesis output")
        _read_receipt(config, receipt_path)
        if _sha(receipt_path) != receipt_hash:
            raise ValueError("synthesis receipt changed during export")
        _check_report(run / "hls" / "solution1", receipt["target"], receipt["top"])
        if control.is_file():
            shutil.copyfile(control, run / "control.h")
        shutil.copyfile(source / "synthesize.tcl", run / "synthesize.tcl")
        shutil.copyfile(source / "hls/solution1/syn/report/csynth.xml", run / "csynth.xml")
        shutil.copyfile(receipt_path, run / "synthesis_receipt.json")
        manifest = {
            "schema_version": 1, "validation_level": "hls_ip_export",
            "target": receipt["target"], "top": receipt["top"], "tool_version": version,
            "completed_at": datetime.now(timezone.utc).isoformat(), **inventory,
            "ip_directory": directory.relative_to(run).as_posix(), "archive": "ip.zip",
            "archive_sha256": _sha(archive),
            "component_xml_sha256": _sha(directory / "component.xml"),
            "control_header": "control.h" if control.is_file() else None,
            "control_header_sha256": _sha(run / "control.h") if control.is_file() else None,
            "csynth_report": "csynth.xml", "csynth_report_sha256": _sha(run / "csynth.xml"),
            "synthesis_receipt_sha256": receipt_hash,
            "input_hashes": receipt["input_hashes"], "package_hashes": _hash_tree(directory),
            "synthesized_verilog_hashes": rtl_hashes,
            "export_tcl_sha256": _sha(script),
            "synthesis_tcl_sha256": _sha(run / "synthesize.tcl"), "log": log.name,
        }
        manifest_path = run / "ip_manifest.json"
        _write_json(manifest_path, manifest)  # Published only after every validation passes.
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"invalid IP export; artifacts retained at {run}: {exc}") from exc
    return IpExportResult(directory, archive, manifest_path, log)
