"""Tool-free S0c conformance using recorded real Vitis 2025.1 evidence."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET

import pytest

from waveflow.build.hls_control import parse_control_abi
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap
from tests.board.control_abi_helpers import FIXTURES, TOPS, I32, U32, intent

NS = {"s": "http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009"}


def parse(top="scalar_probe", directory=None, regmap=None, bindings=None):
    rm, pb = intent(top)
    p = directory or FIXTURES / top
    return parse_control_abi(p / "control.h", p / "component.xml", regmap or rm,
                             pointer_bindings=pb if bindings is None else bindings)


@pytest.mark.parametrize("top", TOPS)
def test_real_evidence_and_scalar_conformance(top):
    spec = parse(top)
    rm, _ = intent(top)
    assert spec.vlnv == f"waveflow:hls:{top}:1.0"
    assert spec.modeled_fields == tuple(sorted(n for n, f in rm._fields.items() if not f.is_vitis_auto))
    provenance = json.loads((FIXTURES / top / "provenance.json").read_text())
    for name, digest in provenance["hashes"].items():
        p = FIXTURES / name if name.endswith(".cpp") else FIXTURES / top / name
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
    assert spec.control_header_sha256 == provenance["hashes"]["control.h"]
    assert spec.component_xml_sha256 == provenance["hashes"]["component.xml"]
    assert spec.abi_sha256 == parse(top).abi_sha256
    assert spec.to_dict()["validation_level"] == "verified_control_abi"
    with pytest.raises(FrozenInstanceError):
        spec.interface = "changed"


def test_not_an_assumed_stride():
    spec = parse()
    assert (spec.field("wide").offset, spec.field("wide").width) == (0x20, 64)
    assert spec.field("y").offset == 0x2c
    rm, _ = intent("scalar_probe")
    user = {n: replace(f, offset=None) for n, f in rm._fields.items() if not f.is_vitis_auto}
    auto = VitisRegMap(user)
    # Upstream now places scalar outputs at the actual Vitis offset.
    assert auto.offset_of("y") == spec.field("y").offset
    auto._offsets["y"] += 4
    with pytest.raises(ValueError, match="offset mismatch: y"):
        parse(regmap=auto)


def test_array_is_a_region_not_a_RegField():
    f = parse("array_probe").field("values")
    assert (f.kind, f.offset, f.width, f.count, f.stride) == ("array", 0x20, 32, 8, 4)


@pytest.mark.parametrize("change,match", [(None, None), ("depth", "depth mismatch"),
    ("width", "element width mismatch"), ("name", "missing from ABI"),
    ("access", "array access mismatch"), ("offset", "array offset mismatch")])
def test_modeled_array_conformance_without_scalar_stride(change, match):
    from waveflow.hw.dataschema import DataArray
    shape = (4,) if change == "depth" else (8,)
    element = U32.specialize(bitwidth=64) if change == "width" else U32
    array = DataArray.specialize(element, max_shape=shape, static=True)
    fields = {"sum": RegField(U32, RegAccess.R),
              "wrong" if change == "name" else "values": RegField(
                  array, RegAccess.R if change == "access" else RegAccess.RW,
                  offset=0x40 if change == "offset" else None)}
    if match:
        with pytest.raises(ValueError, match=match):
            parse("array_probe", regmap=VitisRegMap(fields))
    else:
        spec = parse("array_probe", regmap=VitisRegMap(fields))
        assert spec.field("values").offset == 0x20
        assert spec.modeled_fields == ("sum", "values")


def test_pointer_is_explicit_and_64_bit():
    spec = parse("pointer_probe")
    f = spec.field("buffer_r")
    assert (f.kind, f.offset, f.width, f.bus_interface) == ("pointer", 0x10, 64, "m_axi_gmem")
    assert spec.field("count").offset == 0x1c
    with pytest.raises(ValueError, match="scalar names differ"):
        parse("pointer_probe", bindings={})
    with pytest.raises(ValueError, match="missing AXI master"):
        parse("pointer_probe", bindings={"buffer_r": "s_axi_control"})
    with pytest.raises(ValueError, match="unknown pointer binding"):
        parse("pointer_probe", bindings={"buffer_r": "m_axi_gmem", "nonexistent": "m_axi_gmem"})


def test_control_effects_are_not_pysim_simplifications():
    spec = parse()
    assert spec.field("ap_start").effects == ("SC",)
    assert spec.field("ap_done").effects == ("COR",)
    assert spec.field("ap_ready").effects == ("COR",)
    assert spec.field("ap_idle").effects == ()  # XML's 'modify' is not automatically COR!
    assert spec.field("isr").effects == ("TOW",)
    assert spec.field("y_ap_vld").effects == ("COR",)
    assert spec.field("x").access == "RW"  # actual control.h readback, not XML 'write-only'
    assert spec.field("gain").encoding == "float"
    assert spec.field("x").encoding == "int"
    assert parse("array_probe").field("ap_start").effects == ("COH",)


@pytest.mark.parametrize("change,match", [
    ("name", "scalar names differ"), ("width", "width mismatch"),
    ("access", "access mismatch"), ("offset", "offset mismatch"),
    ("packed", "packed user"),
])
def test_modeled_mismatches_fail(change, match):
    rm, _ = intent("scalar_probe")
    fields = {n: replace(f) for n, f in rm._fields.items() if not f.is_vitis_auto}
    if change == "name": fields["wrong"] = fields.pop("x")
    if change == "width": fields["x"].schema = I32.specialize(bitwidth=16)
    if change == "access": fields["x"].access = RegAccess.R
    if change == "offset": fields["x"].offset = 0x40
    if change == "packed": fields["x"] = RegField(U32, RegAccess.RW, offset=0x10, bit_offset=0)
    with pytest.raises(ValueError, match=match):
        parse(regmap=VitisRegMap(fields))


@pytest.fixture
def evidence(tmp_path):
    shutil.copytree(FIXTURES / "scalar_probe", tmp_path / "evidence")
    return tmp_path / "evidence"


@pytest.mark.parametrize("old,new,match", [
    ("#define CONTROL_BITS_X_DATA    32", "#define CONTROL_BITS_X_DATA    64", "bit layout mismatch"),
    ("#define CONTROL_ADDR_X_DATA    0x10", "#define CONTROL_ADDR_X_DATA    0x14", "missing scalar"),
    ("#define CONTROL_BITS_X_DATA    32", "#define CONTROL_BITS_X_DATA    (32)", "numeric ABI"),
    ("#define CONTROL_BITS_X_DATA    32", "", "unrecognized control macros"),
    ("ap_done (Read/COR)", "ap_done (Read)", "control semantics"),
    ("ap_start (Read/Write/SC)", "ap_start (Read/Write/MAGIC)", "access flags"),
    ("wide[63:32]", "wide[31:0]", "bit layout mismatch"),
])
def test_bad_header_rejected(evidence, old, new, match):
    p = evidence / "control.h"
    data = p.read_text()
    assert old in data
    p.write_text(data.replace(old, new))
    with pytest.raises(ValueError, match=match): parse(directory=evidence)


def test_duplicate_macro_rejected(evidence):
    p = evidence / "control.h"
    p.write_text(p.read_text() + "\n#define CONTROL_BITS_X_DATA 32\n")
    with pytest.raises(ValueError, match="duplicate control macro"): parse(directory=evidence)


@pytest.mark.parametrize("change,match", [
    ("offset", "name/description mismatch|missing"), ("width", "bit width mismatch"),
    ("access", "scalar access mismatch"), ("overlap", "overlapping"),
    ("bundle", "exactly one"), ("version", "2025.1"), ("namespace", "namespace"),
    ("missing", "unrecognized control macros|missing"),
])
def test_bad_component_rejected(evidence, change, match):
    p = evidence / "component.xml"
    tree = ET.parse(p)
    root = tree.getroot()
    regs = root.findall("s:memoryMaps/s:memoryMap/s:addressBlock/s:register", NS)
    x = next(r for r in regs if r.findtext("s:name", namespaces=NS) == "x")
    if change == "offset": x.find("s:addressOffset", NS).text = "20"
    if change == "width": x.find("s:field/s:bitWidth", NS).text = "16"
    if change == "access": x.find("s:access", NS).text = "read-only"
    if change == "overlap": x.find("s:addressOffset", NS).text = "0"
    if change == "bundle": root.find("s:busInterfaces/s:busInterface/s:name", NS).text = "s_axi_other"
    if change == "version":
        for e in root.iter():
            if e.tag.endswith("}xilinxVersion"): e.text = "2024.1"
    if change == "namespace": root.tag = "component"
    if change == "missing": root.find("s:memoryMaps/s:memoryMap/s:addressBlock", NS).remove(x)
    tree.write(p, encoding="unicode")
    with pytest.raises(ValueError, match=match): parse(directory=evidence)


@pytest.mark.parametrize("old,new", [("0x3f", "0x1f"), ("CONTROL_DEPTH_VALUES     8", "CONTROL_DEPTH_VALUES     9"),
                                      ("CONTROL_WIDTH_VALUES     32", "CONTROL_WIDTH_VALUES     16")])
def test_bad_array_bounds_fail(tmp_path, old, new):
    shutil.copytree(FIXTURES / "array_probe", tmp_path / "e")
    p = tmp_path / "e/control.h"
    assert old in p.read_text()
    p.write_text(p.read_text().replace(old, new))
    with pytest.raises(ValueError): parse("array_probe", directory=p.parent)


def test_missing_or_invalid_evidence(evidence):
    with pytest.raises(ValueError, match="VitisRegMap"):
        parse_control_abi(evidence / "control.h", evidence / "component.xml", {})
    (evidence / "component.xml").write_text("<broken")
    with pytest.raises(ValueError, match="invalid component"): parse(directory=evidence)


@pytest.mark.parametrize("kind", ["scalar-access", "scalar-write", "scalar-read", "start-write", "isr-write"])
def test_explicit_incompatible_xml_side_effects_are_not_normalized(evidence, kind):
    p = evidence / "component.xml"
    tree = ET.parse(p)
    regs = tree.getroot().findall("s:memoryMaps/s:memoryMap/s:addressBlock/s:register", NS)
    selected = {"scalar-access": "x", "scalar-write": "x", "scalar-read": "x",
                "start-write": "CTRL", "isr-write": "IP_ISR"}[kind]
    reg = next(r for r in regs if r.findtext("s:name", namespaces=NS) == selected)
    field = reg.find("s:field", NS)
    if kind == "scalar-access":
        field.find("s:access", NS).text = "read-only"
    else:
        tag = "readAction" if kind == "scalar-read" else "modifiedWriteValue"
        node = field.find("s:" + tag, NS)
        if node is None:
            node = ET.SubElement(field, "{" + NS["s"] + "}" + tag)
        node.text = "clear" if kind == "scalar-read" else "oneToClear"
    tree.write(p, encoding="unicode")
    with pytest.raises(ValueError):
        parse(directory=evidence)


def test_compact_enum_uses_current_kernel_boundary_width(evidence):
    """An enum's compact stream width differs from its byte-wide C++ argument."""
    from enum import IntEnum
    from waveflow.hw.dataschema import EnumField
    class Error(IntEnum):
        OK = 0
        EARLY = 1
        MISSING = 2
    schema = EnumField.specialize(enum_type=Error)
    assert schema.get_bitwidth() == 2
    rm, _ = intent("scalar_probe")
    rm._fields["y"] = replace(rm._fields["y"], schema=schema)
    header = evidence / "control.h"
    text = header.read_text()
    # Synthetic derivative of the fixture, for this test only.
    import re
    text = re.sub(r"(CONTROL_BITS_Y_DATA\s+)32", r"\g<1>8", text)
    text = text.replace("y[31:0]", "y[7:0]").replace("bit 31~0 - y", "bit 7~0 - y")
    header.write_text(text)
    component = evidence / "component.xml"
    tree = ET.parse(component)
    regs = tree.getroot().findall("s:memoryMaps/s:memoryMap/s:addressBlock/s:register", NS)
    y = next(r for r in regs if r.findtext("s:name", namespaces=NS) == "y")
    y.find("s:field/s:bitWidth", NS).text = "8"
    tree.write(component, encoding="unicode")
    assert parse(directory=evidence, regmap=rm).field("y").width == 8
    rm._fields["y"] = replace(rm._fields["y"], schema=EnumField.specialize(enum_type=Error, bitwidth=9))
    with pytest.raises(ValueError, match="enum values exceed"):
        parse(directory=evidence, regmap=rm)
