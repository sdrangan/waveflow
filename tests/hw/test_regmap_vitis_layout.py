"""VitisRegMap's argument layout against the s_axilite map Vitis HLS really generates.

``VitisRegMap`` claims to place user fields where Vitis puts them.  Nothing in the build flow
checks that — no generated C++ carries an offset, and the Python sim decodes with the same table
it encodes with — so a wrong layout passes every other test.  This file is the check.

Each probe below is a small kernel whose ``s_axilite`` map was measured by csynth (Vitis HLS
2025.1, ``xc7z020clg484-1``, one ``control`` bundle) and pinned in ``offsets``:

- the unit tests build the matching ``VitisRegMap`` and compare it to the pinned offsets — they
  run everywhere;
- the ``-m vitis`` test re-synthesizes every probe and compares the model against the
  ``ADDR_*`` localparams in the generated ``<top>_control_s_axi.v``, so a Vitis version that moves
  the layout fails here rather than on a board.

The probes are chosen to separate the rules (see the ``VitisRegMap`` docstring): the output gap
(``p_c``, ``p_narrow_out``, ``q_o64_then_in``), width scaling of both the stride and the gap
(``width_probe``, ``p_wide``), array regions and their alignment (``p_arr``, ``q_arr6``,
``q_two_arr``), and first-fit hole filling (``r_ff``, ``r_ff2``).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.regmap import Bit, RegAccess, RegField, VitisRegMap
from waveflow.toolchain import toolchain

_SCALARS = {
    "char":        IntField.specialize(bitwidth=8, signed=True),
    "short":       IntField.specialize(bitwidth=16, signed=True),
    "int":         IntField.specialize(bitwidth=32, signed=True),
    "long long":   IntField.specialize(bitwidth=64, signed=True),
    "float":       FloatField.specialize(bitwidth=32),
    "ap_uint<1>":  Bit,
    "ap_uint<8>":  IntField.specialize(bitwidth=8, signed=False),
    "ap_uint<16>": IntField.specialize(bitwidth=16, signed=False),
    "ap_int<128>": IntField.specialize(bitwidth=128, signed=True),
}

#: (top, [(name, c_type, direction)], {name: measured offset}, body).
#: direction: "in" (read), "out" (written), or "arr" (a read-only array; c_type is "T[n]").
PROBES = [
    ("simp_fun",
     [("x", "int", "in"), ("a", "int", "in"), ("b", "int", "in"), ("y", "int", "out")],
     {"x": 0x10, "a": 0x18, "b": 0x20, "y": 0x28},
     "y = a * x + b;"),
    ("p_c",
     [("a", "int", "in"), ("o32a", "int", "out"), ("o32b", "int", "out"),
      ("llin", "long long", "in"), ("o64", "long long", "out")],
     {"a": 0x10, "o32a": 0x18, "o32b": 0x28, "llin": 0x38, "o64": 0x44},
     "o32a = a; o32b = a + 1; o64 = llin;"),
    ("width_probe",
     [("c8", "char", "in"), ("s16", "short", "in"), ("i32", "int", "in"),
      ("ll64", "long long", "in"), ("out32", "int", "out"), ("out64", "long long", "out")],
     {"c8": 0x10, "s16": 0x18, "i32": 0x20, "ll64": 0x28, "out32": 0x34, "out64": 0x44},
     "out32 = c8 + s16 + i32; out64 = ll64 + 1;"),
    ("p_narrow_out",
     [("a", "int", "in"), ("o8", "char", "out"), ("o16", "short", "out"), ("b", "int", "in")],
     {"a": 0x10, "o8": 0x18, "o16": 0x28, "b": 0x38},
     "o8 = a + b; o16 = a - b;"),
    ("q_o64_then_in",
     [("a", "int", "in"), ("o64", "long long", "out"), ("b", "int", "in"), ("y", "int", "out")],
     {"a": 0x10, "o64": 0x18, "b": 0x30, "y": 0x38},
     "o64 = a; y = b;"),
    ("p_wide",
     [("w_in", "ap_int<128>", "in"), ("a", "int", "in"),
      ("w_out", "ap_int<128>", "out"), ("y", "int", "out")],
     {"w_in": 0x10, "a": 0x24, "w_out": 0x2c, "y": 0x54},
     "w_out = w_in + a; y = a;"),
    # poly's shape: three narrow outputs, then the coefficient array.
    ("p_poly",
     [("halted", "ap_uint<1>", "out"), ("error", "ap_uint<8>", "out"),
      ("tx_id", "ap_uint<16>", "out"), ("coeffs", "float[4]", "arr")],
     {"halted": 0x10, "error": 0x20, "tx_id": 0x30, "coeffs": 0x40},
     "halted = coeffs[0] > 0; error = coeffs[1]; tx_id = coeffs[2] + coeffs[3];"),
    ("p_arr",
     [("a", "int", "in"), ("coeffs", "int[4]", "arr"), ("b", "int", "in"), ("y", "int", "out")],
     {"a": 0x10, "coeffs": 0x20, "b": 0x18, "y": 0x30},
     "y = a * coeffs[0] + b * coeffs[3];"),
    ("q_arr_first",
     [("coeffs", "int[4]", "arr"), ("a", "int", "in"), ("y", "int", "out")],
     {"coeffs": 0x10, "a": 0x20, "y": 0x28},
     "y = a * coeffs[0] + coeffs[3];"),
    ("q_arr2",
     [("a", "int", "in"), ("arr", "int[2]", "arr"), ("b", "int", "in"), ("y", "int", "out")],
     {"a": 0x10, "arr": 0x18, "b": 0x20, "y": 0x28},
     "y = a * arr[0] + b * arr[1];"),
    ("q_arr6",
     [("a", "int", "in"), ("arr", "int[6]", "arr"), ("b", "int", "in"), ("y", "int", "out")],
     {"a": 0x10, "arr": 0x20, "b": 0x18, "y": 0x40},
     "y = a * arr[0] + b * arr[5];"),
    ("q_two_arr",
     [("a", "int", "in"), ("x", "int[4]", "arr"), ("v", "int[8]", "arr"),
      ("b", "int", "in"), ("z", "int", "out")],
     {"a": 0x10, "x": 0x20, "v": 0x40, "b": 0x18, "z": 0x30},
     "z = a * x[0] + b * v[7];"),
    ("q_out_hole",
     [("a", "int", "in"), ("y", "int", "out"), ("b", "int", "in"), ("arr", "int[4]", "arr"),
      ("c", "int", "in"), ("z", "int", "out")],
     {"a": 0x10, "y": 0x18, "b": 0x28, "arr": 0x30, "c": 0x40, "z": 0x48},
     "y = a + arr[0]; z = b + c + arr[3];"),
    # First fit: b (declared after y) takes the hole before the array that y did not fit.
    ("r_ff",
     [("a", "int", "in"), ("arr", "int[4]", "arr"), ("y", "int", "out"), ("b", "int", "in")],
     {"a": 0x10, "arr": 0x20, "y": 0x30, "b": 0x18},
     "y = a * arr[0] + b;"),
    ("r_ff2",
     [("a", "int", "in"), ("arr", "int[4]", "arr"), ("ll", "long long", "in"),
      ("b", "int", "in"), ("y", "int", "out")],
     {"a": 0x10, "arr": 0x20, "ll": 0x30, "b": 0x18, "y": 0x3c},
     "y = a * arr[0] + b + ll;"),
]


def _regmap_for(ports) -> VitisRegMap:
    fields = {}
    for name, ctype, direction in ports:
        m = re.fullmatch(r"(.+)\[(\d+)\]", ctype)
        if m:
            schema = DataArray.specialize(
                _SCALARS[m.group(1)], max_shape=(int(m.group(2)),), cpp_storage="raw",
            )
        else:
            schema = _SCALARS[ctype]
        access = RegAccess.R if direction == "out" else RegAccess.RW
        fields[name] = RegField(schema, access)
    return VitisRegMap(fields)


# ---------------------------------------------------------------------------
# Unit tests — the model against the pinned measurements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("top,ports,offsets,body", PROBES, ids=[p[0] for p in PROBES])
def test_layout_matches_measured_map(top, ports, offsets, body) -> None:
    rm = _regmap_for(ports)
    got = {name: rm.offset_of(name) for name, _, _ in ports}
    assert got == offsets, (
        f"{top}: model {{{', '.join(f'{k}: 0x{v:02x}' for k, v in got.items())}}} "
        f"!= Vitis {{{', '.join(f'{k}: 0x{v:02x}' for k, v in offsets.items())}}}"
    )


def test_rejects_array_of_non_32_bit_elements() -> None:
    """Vitis's region layout for narrower elements has not been measured, so the model refuses
    rather than guessing."""
    arr = DataArray.specialize(_SCALARS["short"], max_shape=(4,), cpp_storage="raw")
    with pytest.raises(ValueError, match="32-bit elements"):
        VitisRegMap({"arr": RegField(arr, RegAccess.RW)})


# ---------------------------------------------------------------------------
# Vitis conformance — re-measure every probe
# ---------------------------------------------------------------------------


def _kernel_source(top, ports, body) -> str:
    args, pragmas = [], []
    for name, ctype, direction in ports:
        m = re.fullmatch(r"(.+)\[(\d+)\]", ctype)
        if m:
            args.append(f"{m.group(1)} {name}[{m.group(2)}]")
        else:
            # Inputs by reference too, as hwgen.kernel_signature emits them: Vitis infers the
            # direction from use, so a read-only reference is an input.
            args.append(f"{ctype} &{name}")
        pragmas.append(f"#pragma HLS INTERFACE s_axilite port={name} bundle=control")
    pragmas.append("#pragma HLS INTERFACE s_axilite port=return bundle=control")
    return (
        f"void {top}({', '.join(args)}) {{\n" + "\n".join(pragmas) + f"\n    {body}\n}}\n"
    )


def _parse_addr_map(verilog: str) -> dict[str, int]:
    """``ADDR_<NAME>_(DATA_0|BASE) = N'hXX`` → ``{name: offset}`` (lower-cased), plus the
    control block's ``ADDR_AP_CTRL`` / ``GIE`` / ``IER`` / ``ISR``."""
    out: dict[str, int] = {}
    for name, suffix, hexval in re.findall(
        r"ADDR_(\w+?)(_DATA_0|_BASE|)\s*=\s*\d+'h([0-9a-fA-F]+)", verilog,
    ):
        if suffix or name in ("AP_CTRL", "GIE", "IER", "ISR"):
            out[name.lower()] = int(hexval, 16)
    return out


@pytest.fixture(scope="module")
def vitis_maps(tmp_path_factory) -> dict[str, dict[str, int]]:
    """csynth every probe once; return ``{top: parsed ADDR_* map}``."""
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    root = tmp_path_factory.mktemp("regmap_layout")
    src = '#include <ap_int.h>\n\n' + "\n".join(
        _kernel_source(top, ports, body) for top, ports, _, body in PROBES
    )
    (root / "probes.cpp").write_text(src, encoding="utf-8")
    tcl = []
    for top, *_ in PROBES:
        tcl.append(
            f"open_project -reset proj_{top}\nset_top {top}\nadd_files probes.cpp\n"
            f"open_solution -reset solution1\nset_part {{xc7z020clg484-1}}\n"
            f"create_clock -period 10\n"
            f"if {{[catch {{csynth_design}} res]}} {{ puts $res; exit 1 }}\nclose_project\n"
        )
    (root / "run.tcl").write_text("".join(tcl) + "exit 0\n", encoding="utf-8")
    try:
        toolchain.run_vitis_hls(root / "run.tcl", work_dir=root)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"csynth of the regmap probes failed\nrc={exc.returncode}\n{exc.stdout}")
    maps = {}
    for top, *_ in PROBES:
        # csynth writes syn/verilog/; impl/ would be stale from an export.
        (v,) = (root / f"proj_{top}" / "solution1" / "syn" / "verilog").glob("*_control_s_axi.v")
        maps[top] = _parse_addr_map(v.read_text(encoding="utf-8", errors="replace"))
    return maps


@pytest.mark.vitis
@pytest.mark.parametrize("top,ports,offsets,body", PROBES, ids=[p[0] for p in PROBES])
def test_layout_matches_vitis_rtl(vitis_maps, top, ports, offsets, body) -> None:
    rtl = vitis_maps[top]
    rm = _regmap_for(ports)
    # A broken parse would make every comparison below vacuous.
    assert rtl.get("ap_ctrl") == 0x00, f"{top}: no ADDR_AP_CTRL parsed from {rtl}"
    assert (rtl["gie"], rtl["ier"], rtl["isr"]) == (
        rm.offset_of("gier"), rm.offset_of("ier"), rm.offset_of("isr"),
    )
    model = {name: rm.offset_of(name) for name, _, _ in ports}
    vitis = {name: rtl.get(name) for name, _, _ in ports}
    assert model == vitis, f"{top}: model {model} != Vitis RTL {vitis}"
    # And the pins above are still what this Vitis produces.
    assert vitis == offsets, f"{top}: Vitis moved the layout; re-measure the pins"
