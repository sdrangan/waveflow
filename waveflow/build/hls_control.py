"""Verified Vitis 2025.1 AXI-Lite ABI, independent of any application.

Offsets/widths come from control.h AND IP-XACT, never RegMap placement formulas.
The header is authoritative for bus readback and side effects: Vitis IP-XACT
labels readable input registers write-only and marks even ordinary reads 'modify'.
Only these measured 2025.1 conventions are normalized; other mismatches fail.
Multiple control bundles and packed/sub-word array layouts are rejected explicitly.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping

from waveflow.build.build import BuildConfig
from waveflow.build.ip_export import _board_target, _hash_tree, _inside, _period_matches, _sha
from waveflow.hw.dataschema import DataArray, EnumField, FloatField, IntField
from waveflow.hw.regmap import VitisRegMap


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _number(text):
    _require(isinstance(text, str) and re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", text),
             f"unsupported or missing numeric ABI value: {text!r}")
    return int(text, 16 if text.startswith("0x") else 10)


def _name(text):
    _require(isinstance(text, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text),
             f"invalid ABI name: {text!r}")
    return text


def _xml(path):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"invalid component.xml: {path}") from exc
    _require(root.tag == "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}component",
             "unsupported IP-XACT namespace")
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


@dataclass(frozen=True)
class ControlField:
    name: str
    kind: str                 # scalar, pointer, array, control, valid
    offset: int               # byte offset relative to the control interface
    width: int
    access: str               # R, RW, W, TOW
    bit_offset: int = 0
    effects: tuple[str, ...] = ()
    count: int = 1
    stride: int = 0
    encoding: str = "uint"    # raw unsigned for pointers/arrays; schema-derived for scalars
    bus_interface: str | None = None
    write_mask: int | None = None


@dataclass(frozen=True)
class HlsControlSpec:
    vlnv: str
    interface: str
    address_range: int
    fields: tuple[ControlField, ...]
    control_header_sha256: str
    component_xml_sha256: str
    modeled_fields: tuple[str, ...]
    tool_version: str = "2025.1"

    def field(self, name: str) -> ControlField:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {"schema_version": 1, "validation_level": "verified_control_abi", **asdict(self)}

    @property
    def abi_sha256(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _header(path: Path, prefix: str):
    text = path.read_text(encoding="utf-8")
    macros, sections = {}, {}
    current = None
    for line in text.splitlines():
        if line.startswith("#define "):
            match = re.fullmatch(r"#define\s+(\w+)\s+(\S+)\s*", line)
            _require(match is not None, "malformed control macro")
            key, value = match.groups()
            _require(key.startswith(prefix + "_"), "control macro bundle mismatch")
            key = key[len(prefix) + 1:]
            _require(key not in macros, f"duplicate control macro {key}")
            macros[key] = _number(value)
        match = re.fullmatch(r"//\s*(0x[\da-fA-F]+)\s*:\s*(.*)", line)
        if match:
            current = _number(match[1])
            _require(current not in sections, f"duplicate control address {current:#x}")
            sections[current] = {"description": match[2], "bits": []}
        bit = re.fullmatch(r"//\s*bit\s+(\d+)(?:~(\d+))?\s*-\s*(.*?)\s*\(([^)]+)\)\s*", line)
        if bit:
            _require(current is not None, "bit outside a control register")
            hi, lo = int(bit[1]), int(bit[2] or bit[1])
            _require(0 <= lo <= hi < 32, "invalid control bit range")
            flags = bit[4].split("/")
            _require(len(set(flags)) == len(flags)
                     and set(flags) <= {"Read", "Write", "COR", "SC", "COH", "TOW"}
                     and ("Read" in flags or "Write" in flags), "unsupported control access flags")
            access = "TOW" if "TOW" in flags else (
                "RW" if "Read" in flags and "Write" in flags else "R" if "Read" in flags else "W")
            effects = tuple(f for f in flags if f not in ("Read", "Write"))
            sections[current]["bits"].append((lo, hi - lo + 1, bit[3], access, effects))
    _require(macros and sections, "missing control ABI macros/comments")
    return text, macros, sections


def _codec(schema):
    if issubclass(schema, FloatField):
        _require(schema.get_bitwidth() in (32, 64), "unsupported float width")
        return "float"
    if issubclass(schema, IntField):
        return "int" if schema.signed else "uint"
    if issubclass(schema, EnumField):
        return "uint"
    raise ValueError("only integer, enum and float scalar schemas are supported; arrays are ABI regions")


def _scalar_width(schema):
    # Follow the current kernel boundary lowering: enums use ap_uint<8>,
    # independently of their compact stream-serialization width.
    if issubclass(schema, EnumField):
        from waveflow.build.hwgen import cpp_type
        match = re.fullmatch(r"ap_uint<(\d+)>", cpp_type(schema))
        _require(match is not None, "unsupported enum kernel representation")
        width = int(match[1])
        _require(schema.get_bitwidth() <= width, "enum values exceed kernel representation")
        return width
    return schema.get_bitwidth()


def parse_control_abi(
    control_header: str | Path, component_xml: str | Path, regmap: VitisRegMap, *,
    interface: str = "s_axi_control", pointer_bindings: Mapping[str, str] | None = None,
) -> HlsControlSpec:
    """Parse one control bundle against scalar and optional array design intent.

    pointer_bindings maps the *actual generated register name* to an m_axi bus
    interface. IP-XACT does not identify which 64-bit scalar is a pointer, so this
    intent must be explicit, never guessed from a name or width. The binding's
    register width and master interface/address width are checked. This does not
    establish board DDR wiring, allocation or cache coherence.
    """
    _require(isinstance(regmap, VitisRegMap), "conformance requires a VitisRegMap")
    header, component = Path(control_header), Path(component_xml)
    header_hash, component_hash = _sha(header), _sha(component)
    root = _xml(component)
    _require(root.findtext("vendorExtensions/packagingInfo/xilinxVersion") == "2025.1",
             "control ABI parser requires Vitis 2025.1 metadata")
    vlnv = ":".join(root.findtext(k, "") for k in ("vendor", "library", "name", "version"))
    _require(all(vlnv.split(":")), "missing IP identity")
    buses = {}
    for bus in root.findall("busInterfaces/busInterface"):
        name = _name(bus.findtext("name"))
        _require(name not in buses, "duplicate bus interface")
        buses[name] = bus
    slaves = [name for name, b in buses.items() if b.find("slave") is not None
              and b.find("busType") is not None
              and b.find("busType").get("{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}name") == "aximm"]
    _require(slaves == [interface], "exactly one AXI-Lite control bundle is supported")
    params = {p.findtext("name"): p.findtext("value")
              for p in buses[interface].findall("parameters/parameter")}
    _require(params.get("PROTOCOL") == "AXI4LITE" and params.get("DATA_WIDTH") == "32",
             "control interface must be 32-bit AXI4-Lite")
    ref = buses[interface].find("slave/memoryMapRef")
    ref_name = None if ref is None else ref.get(
        "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}memoryMapRef")
    maps = [m for m in root.findall("memoryMaps/memoryMap") if m.findtext("name") == ref_name]
    _require(len(maps) == 1, "missing or ambiguous control memory map")
    blocks = maps[0].findall("addressBlock")
    _require(len(blocks) == 1, "exactly one control address block is supported")
    block = blocks[0]
    _require(_number(block.findtext("baseAddress")) == 0
             and _number(block.findtext("width")) == 32, "unsupported control address block")
    address_bits = _number(params.get("ADDR_WIDTH"))
    _require(2 <= address_bits <= 32, "unsupported control address width")
    address_range = min(_number(block.findtext("range")), 1 << address_bits)
    regs = {}
    for r in block.findall("register"):
        off = _number(r.findtext("addressOffset"))
        _require(off % 4 == 0 and off not in regs and off + 4 <= address_range,
                 "unaligned, overlapping or out-of-range IP-XACT register")
        # Vitis uses byte-region size for Memory_* entries, 32-bit words elsewhere.
        if not r.findtext("description", "").startswith("Memory "):
            _require(_number(r.findtext("size")) == 32, "unsupported register word size")
        _require(r.findtext("access") in {"read-only", "write-only", "read-write"},
                 "unsupported register access")
        claimed_bits, field_names = 0, set()
        for xml_field in r.findall("field"):
            field_name = _name(xml_field.findtext("name"))
            lo, width = _number(xml_field.findtext("bitOffset")), _number(xml_field.findtext("bitWidth"))
            _require(field_name not in field_names and width > 0 and lo + width <= 32,
                     "invalid or duplicate IP-XACT bit field")
            mask = ((1 << width) - 1) << lo
            _require(not claimed_bits & mask, "overlapping IP-XACT bit fields")
            claimed_bits |= mask
            field_names.add(field_name)
        regs[off] = r
    prefix = interface.removeprefix("s_axi_").upper()
    text, macros, sections = _header(header, prefix)
    used_macros, used_regs = set(), set()

    def macro(key):
        _require(key in macros, f"missing control macro {key}")
        used_macros.add(key)
        return macros[key]

    def reg(off, description):
        _require(off in regs, f"IP-XACT register missing at {off:#x}")
        node = regs[off]
        _require(node.findtext("description") == description, f"IP-XACT name/description mismatch at {off:#x}")
        used_regs.add(off)
        return node

    fields = []
    ctrl_offset = macro("ADDR_AP_CTRL")
    ctrl = reg(ctrl_offset, "Control signals")
    _require(ctrl.findtext("access") == "read-write", "control register access mismatch")
    ctrl_bits = sections.get(ctrl_offset, {}).get("bits", [])
    _require({b[2] for b in ctrl_bits} == {
        "ap_start", "ap_done", "ap_idle", "ap_ready", "auto_restart", "interrupt"},
        "unsupported or incomplete ap_ctrl_hs layout")
    for lo, width, name, access, effects in ctrl_bits:
        xf = [f for f in ctrl.findall("field") if f.findtext("name") == name.upper()]
        _require(len(xf) == 1 and _number(xf[0].findtext("bitOffset")) == lo
                 and _number(xf[0].findtext("bitWidth")) == width == 1,
                 f"control bit mismatch: {name}")
        expected = "read-write" if access == "RW" else "read-only"
        _require(xf[0].findtext("access") == expected, f"control access mismatch: {name}")
        if access == "RW":
            _require(xf[0].findtext("modifiedWriteValue") == "modify",
                     f"control write-action mismatch: {name}")
        if "COR" in effects:
            _require(xf[0].findtext("readAction") in ("modify", "clear"),
                     f"control read-action mismatch: {name}")
        _require((name == "ap_start" and access == "RW" and effects in (("SC",), ("COH",)))
                 or (name in ("ap_done", "ap_ready") and access == "R" and effects == ("COR",))
                 or (name == "auto_restart" and access == "RW" and not effects)
                 or (name in ("ap_idle", "interrupt") and access == "R" and not effects),
                 f"unsupported control semantics: {name}")
        fields.append(ControlField(name, "control", ctrl_offset, width, access, lo, effects))
    for name, suffix, desc in (("gier", "GIE", "Global Interrupt Enable Register"),
                               ("ier", "IER", "IP Interrupt Enable Register"),
                               ("isr", "ISR", "IP Interrupt Status Register")):
        off = macro("ADDR_" + suffix)
        xr = reg(off, desc)
        _require(xr.findtext("access") == "read-write", f"interrupt register access mismatch: {name}")
        bits = sections.get(off, {}).get("bits", [])
        access = "TOW" if name == "isr" else "RW"
        _require(bits and all(b[3] == access for b in bits), f"invalid interrupt access: {name}")
        mask = 0
        for lo, width, _, _, _ in bits:
            bitmask = ((1 << width) - 1) << lo
            _require(not mask & bitmask, "overlapping interrupt bits")
            mask |= bitmask
            xf = [f for f in xr.findall("field") if not f.findtext("name", "").startswith("RESERVED")
                  and _number(f.findtext("bitOffset")) == lo and _number(f.findtext("bitWidth")) == width]
            _require(len(xf) == 1, f"interrupt bit mismatch: {name}")
            if name == "isr":
                _require(xf[0].findtext("modifiedWriteValue") == "oneToToggle", "ISR must be toggle-on-write")
            else:
                _require(xf[0].findtext("access") == "read-write"
                         and xf[0].findtext("modifiedWriteValue") == "modify",
                         "interrupt enable must be ordinarily writable")
        fields.append(ControlField(name, "control", off, 32, access,
                                   effects=("TOW",) if name == "isr" else (), write_mask=mask))

    pointers = dict(pointer_bindings or {})
    for key in sorted(macros):
        if not (key.startswith("BITS_") and key.endswith("_DATA")):
            continue
        token = key[5:-5]
        width, off = macro(key), macro("ADDR_" + token + "_DATA")
        _require(0 < width <= 4096, "unsupported scalar width")
        desc = sections.get(off, {}).get("description", "")
        _require(desc.startswith("Data signal of "), "missing scalar header description")
        name = _name(desc.removeprefix("Data signal of "))
        _require(name.upper() == token, "scalar name/macro mismatch")
        access, effects = None, None
        for index in range((width + 31) // 32):
            word = off + 4 * index
            bits = sections.get(word, {}).get("bits", [])
            nbits = min(32, width - index * 32)
            label = (f"{name}[{index * 32}]" if nbits == 1 else
                     f"{name}[{index * 32 + nbits - 1}:{index * 32}]")
            _require(len(bits) == 1 and bits[0][:3] == (0, nbits, label),
                     f"scalar header bit layout mismatch: {name}")
            if access is None:
                access, effects = bits[0][3:]
            _require(bits[0][3:] == (access, effects) and access in ("R", "W", "RW") and not effects,
                     f"unsupported scalar access: {name}")
            xr = reg(word, desc)
            # Vitis 2025.1 publishes inputs as write-only in IP-XACT despite
            # documenting/implementing readback in control.h and the RTL.
            allowed = {"R": {"read-only"}, "W": {"write-only"},
                       "RW": {"read-write", "write-only"}}[access]
            _require(xr.findtext("access") in allowed, f"scalar access mismatch: {name}")
            data_bits = [f for f in xr.findall("field") if f.findtext("name") == name]
            _require(len(data_bits) == 1 and _number(data_bits[0].findtext("bitOffset")) == 0
                     and _number(data_bits[0].findtext("bitWidth")) == nbits,
                     f"scalar IP-XACT bit width mismatch: {name}")
            _require(data_bits[0].findtext("access") == xr.findtext("access")
                     and data_bits[0].findtext("modifiedWriteValue") in (None, "modify")
                     and data_bits[0].findtext("readAction") in (None, "modify"),
                     f"unsupported scalar IP-XACT access/action: {name}")
        bus_name = pointers.get(name)
        kind = "pointer" if bus_name is not None else "scalar"
        if bus_name is not None:
            bus = buses.get(bus_name)
            _require(bus is not None and bus.find("master") is not None,
                     f"pointer {name}: missing AXI master {bus_name}")
            bt = bus.find("busType")
            _require(bt is not None and bt.get(
                "{http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009}name") == "aximm",
                "pointer binding must name an AXI memory master")
            bp = {p.findtext("name"): p.findtext("value") for p in bus.findall("parameters/parameter")}
            _require(width == 64 and bp.get("ADDR_WIDTH") == "64" and access == "RW",
                     "pointer requires a writable 64-bit register and 64-bit AXI addresses")
        fields.append(ControlField(name, kind, off, width, access, bus_interface=bus_name))
        ctl_key = "ADDR_" + token + "_CTRL"
        if ctl_key in macros:
            co = macro(ctl_key)
            xr = reg(co, f"Control signal of {name}")
            bits = sections.get(co, {}).get("bits", [])
            _require(len(bits) == 1 and bits[0] == (0, 1, name + "_ap_vld", "R", ("COR",)),
                     f"unsupported output-valid control: {name}")
            xf = [f for f in xr.findall("field") if f.findtext("name") == name + "_ap_vld"]
            _require(len(xf) == 1 and _number(xf[0].findtext("bitOffset")) == 0
                     and _number(xf[0].findtext("bitWidth")) == 1,
                     f"output-valid bit mismatch: {name}")
            _require(xr.findtext("access") == "read-only" and xf[0].findtext("access") == "read-only"
                     and xf[0].findtext("readAction") in ("modify", "clear"),
                     f"output-valid access mismatch: {name}")
            fields.append(ControlField(name + "_ap_vld", "valid", co, 1, "R", effects=("COR",)))

    for key in sorted(macros):
        if not (key.startswith("ADDR_") and key.endswith("_BASE")):
            continue
        token = key[5:-5]
        base, high = macro(key), macro("ADDR_" + token + "_HIGH")
        width, depth = macro("WIDTH_" + token), macro("DEPTH_" + token)
        _require(width in (32, 64) and 0 < depth <= 65536,
                 "only word-aligned 32/64-bit AXI-Lite arrays are supported")
        memories = re.findall(r"Memory '([A-Za-z_][A-Za-z0-9_]*)' \((\d+) \* (\d+)b\)", text)
        candidates = [(n, int(d), int(w)) for n, d, w in memories if n.upper() == token]
        _require(len(candidates) == 1 and candidates[0][1:] == (depth, width),
                 "array header metadata mismatch")
        name = candidates[0][0]
        _require(high - base + 1 >= depth * (width // 8) and high < address_range,
                 "array region bounds mismatch")
        xr = reg(base, "Memory " + name)
        _require(xr.findtext("name") == "Memory_" + name and xr.findtext("access") == "read-write",
                 "array IP-XACT metadata mismatch")
        _require(_number(xr.findtext("size")) == high - base + 1,
                 "array IP-XACT region size mismatch")
        fields.append(ControlField(name, "array", base, width, "RW", count=depth, stride=width // 8))
    _require(used_macros == set(macros), f"unrecognized control macros: {sorted(set(macros) - used_macros)}")
    _require(used_regs == set(regs), "unrecognized IP-XACT registers")
    _require(set(pointers) == {f.name for f in fields if f.kind == "pointer"}, "unknown pointer binding")
    _require(len({f.name for f in fields}) == len(fields), "duplicate ABI field name")
    occupied = {}
    for f in fields:
        span = f.count * f.stride if f.kind == "array" else ((f.width + 31) // 32) * 4
        _require(f.offset % 4 == 0 and f.offset + span <= address_range, "ABI field outside address range")
        for offset in range(f.offset, f.offset + span, 4):
            mask = ((1 << f.width) - 1) << f.bit_offset if f.width < 32 else 0xffffffff
            _require(not occupied.get(offset, 0) & mask, "overlapping ABI fields")
            occupied[offset] = occupied.get(offset, 0) | mask
    modeled = {n: f for n, f in regmap._fields.items() if not f.is_vitis_auto}
    scalars = {n: f for n, f in modeled.items() if not issubclass(f.schema, DataArray)}
    arrays = {n: f for n, f in modeled.items() if issubclass(f.schema, DataArray)}
    actual = {f.name: f for f in fields if f.kind == "scalar"}
    _require(set(scalars) == set(actual),
             f"scalar names differ: modeled={sorted(scalars)}, actual={sorted(actual)}")
    for name, field in scalars.items():
        abi = actual[name]
        _require(regmap.bit_offset_of(name) is None, "packed user scalar registers are unsupported")
        _require(regmap.offset_of(name) == abi.offset, f"scalar offset mismatch: {name}")
        _require(_scalar_width(field.schema) == abi.width, f"scalar width mismatch: {name}")
        _require(field.access.value == abi.access, f"scalar access mismatch: {name}")
        fields[fields.index(abi)] = replace(abi, encoding=_codec(field.schema))
    # Array placement is allocated by HLS, not by the scalar RegMap stride.
    # Preserve support for ABI-only regions (S0c), but check every modeled array.
    regions = {f.name: f for f in fields if f.kind == "array"}
    for name, field in arrays.items():
        _require(name in regions, f"modeled array missing from ABI: {name}")
        abi, schema = regions[name], field.schema
        _require(schema.static and len(schema.max_shape) == 1,
                 f"only static one-dimensional modeled arrays are supported: {name}")
        _require(schema._element_type().get_bitwidth() == abi.width,
                 f"array element width mismatch: {name}")
        _require(schema.max_shape[0] == abi.count, f"array depth mismatch: {name}")
        _require(field.access.value == abi.access, f"array access mismatch: {name}")
        _require(field.bit_offset is None, f"packed array unsupported: {name}")
        if field.offset is not None:
            _require(field.offset == abi.offset, f"array offset mismatch: {name}")
    if arrays:
        _require(set(arrays) == set(regions), "array names differ from modeled array intent")
    for name, field in regmap._fields.items():
        if not field.is_vitis_auto:
            continue
        actual_field = next((f for f in fields if f.name == name), None)
        _require(actual_field is not None and regmap.offset_of(name) == actual_field.offset
                 and (regmap.bit_offset_of(name) or 0) == actual_field.bit_offset
                 and _scalar_width(field.schema) == actual_field.width,
                 f"modeled control layout mismatch: {name}")
    _require(_sha(header) == header_hash and _sha(component) == component_hash, "ABI inputs changed while parsing")
    return HlsControlSpec(vlnv, interface, address_range, tuple(sorted(fields, key=lambda f: (f.offset, f.bit_offset))),
                          header_hash, component_hash, tuple(sorted(modeled)))


def control_spec_from_export(
    config: BuildConfig, manifest_path: str | Path, regmap: VitisRegMap, **kwargs,
) -> HlsControlSpec:
    """Recheck the S0b evidence/target, then parse and verify its control ABI."""
    path = Path(manifest_path).resolve()
    try:
        manifest_hash = _sha(path)
        record = json.loads(path.read_text(encoding="utf-8"))
        _require(record["schema_version"] == 1 and record["validation_level"] == "hls_ip_export",
                 "not an S0b export manifest")
        _require(record["target"] == _board_target(config) and record["tool_version"] == "2025.1",
                 "export target differs from current Board/HLS target")
        root = path.parent
        package = _inside(root, record["ip_directory"])
        _require(_hash_tree(package) == record["package_hashes"], "stale IP package")
        for rel, digest in ((record["archive"], record["archive_sha256"]),
                            (record["csynth_report"], record["csynth_report_sha256"]),
                            ("synthesis_receipt.json", record["synthesis_receipt_sha256"]),
                            ("synthesize.tcl", record["synthesis_tcl_sha256"]),
                            ("export.tcl", record["export_tcl_sha256"])):
            _require(_sha(_inside(root, rel)) == digest, f"stale export evidence: {rel}")
        _require(record["control_header"] is not None, "IP has no control ABI")
        header = _inside(root, record["control_header"])
        component = package / "component.xml"
        _require(_sha(header) == record["control_header_sha256"], "stale control header")
        _require(_sha(component) == record["component_xml_sha256"], "stale component.xml")
        report = ET.parse(_inside(root, record["csynth_report"])).getroot()
        assignments = report.find("UserAssignments")
        _require(assignments is not None, "missing synthesis target evidence")
        target = record["target"]
        _require(assignments.findtext("Part", "").replace("-", "").lower()
                 == target["part"].replace("-", "").lower()
                 and assignments.findtext("TopModelName") == record["top"]
                 and assignments.findtext("FlowTarget") == "vivado"
                 and report.findtext("ReportVersion/Version") == record["tool_version"]
                 and _period_matches(assignments.findtext("TargetClockPeriod"), target["hls_period_ns"]),
                 "actual synthesis report does not match export target")
        receipt = json.loads((root / "synthesis_receipt.json").read_text(encoding="utf-8"))
        _require(receipt["target"] == target and receipt["top"] == record["top"]
                 and receipt["input_hashes"] == record["input_hashes"], "receipt/export provenance mismatch")
        spec = parse_control_abi(header, component, regmap, **kwargs)
        _require(spec.vlnv == record["vlnv"], "export VLNV mismatch")
        _require(_sha(path) == manifest_hash and _hash_tree(package) == record["package_hashes"]
                 and _sha(header) == record["control_header_sha256"],
                 "export evidence changed during ABI verification")
        return spec
    except (OSError, KeyError, TypeError, ET.ParseError, json.JSONDecodeError) as exc:
        raise ValueError(f"missing or invalid export evidence: {path}") from exc
