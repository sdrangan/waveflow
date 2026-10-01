"""Body-only kernels through Vitis: csim, csynth, the register map, and the error path.

The gate for plans/hook_first_flow.md Stage 1.  ``stream_inband``'s poly kernel, rewritten
body-only: the generated top is the pragmas plus one call, and the whole kernel (command
loop, framing, compute, status) is the hand-written hook in
``fixtures/body_only/polyb_run_impl.tpp``.  Every register argument now crosses that call
by reference, which is the thing to prove Vitis still accepts.

Asserted:

1. csim outputs are byte-identical to the original, extracted ``poly`` kernel on the same
   inputs (response header, samples, register status);
2. csynth succeeds;
3. the register offsets Vitis assigns equal Waveflow's ``VitisRegMap`` (an argument that
   crossed the call and lost its binding would move or vanish);
4. on an early TLAST the hook writes ``halted``/``error``/``tx_id`` through the references
   (``fixtures/body_only/polyb_tb_err.cpp``, a hand-written testbench -- the generated one
   always pops ``nsamp`` samples and cannot run an error path).

Measured on 2026-10-01 (Vitis HLS 2025.1): all four hold; II = 1 on the sample loop.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from examples.stream_inband.poly_build import HlsGenIncludeStep
from tests.fixtures.poly_extracted.poly_extracted import (
    PolyAccel,
    PolyTBHls,
    write_legacy_inputs,
)
from waveflow.build.build import BuildConfig
from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen_steps import HlsCodegenStep
from waveflow.build.hwgen import kernel_files_to_str
from waveflow.simulation.simobj import ProcessGen
from waveflow.toolchain import toolchain

pytestmark = pytest.mark.vitis

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "body_only"
STATUS = ("halted", "error", "tx_id")


@dataclass
class PolyBody(PolyAccel):
    """poly, body-only: the same ports and register map, the whole kernel in one hook."""

    cpp_kernel_name: ClassVar[str | None] = "polyb"
    cpp_namespace: ClassVar[str | None] = "polyb_impl"
    cpp_body: ClassVar[str | None] = "run"

    def run(self) -> ProcessGen[None]:
        """Not simulated here; the C++ body is the fixture."""
        return
        yield


def _tcl(top: str, kernel: str, tb: str, data: str, csynth: bool) -> str:
    synth = (
        'if {[catch {csynth_design} res]} { puts "GATE_ERROR csynth: $res"; exit 1 }\n'
        'puts "GATE_CSYNTH_DONE"\n'
    ) if csynth else ""
    return (
        f"open_project -reset {top}_{Path(tb).stem}_proj\n"
        f"set_top {top}\n"
        f'add_files {kernel} -cflags "-I."\n'
        f'add_files -tb {tb} -cflags "-I."\n'
        "add_files -tb include/streamutils.cpp\n"
        'open_solution -reset "solution1"\n'
        "set_part {xc7z020clg484-1}\n"
        "create_clock -period 10\n"
        f"set data_dir [file join [file dirname [file normalize [info script]]] {data}]\n"
        'if {[catch {csim_design -argv "$data_dir"} res]} { puts "GATE_ERROR csim: $res"; exit 1 }\n'
        'puts "GATE_CSIM_DONE"\n'
        f"{synth}exit 0\n"
    )


def _vitis(root: Path, name: str, tcl: str) -> str:
    (root / name).write_text(tcl, encoding="utf-8")
    try:
        r = toolchain.run_vitis_hls(root / name, work_dir=root)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.CalledProcessError as e:
        pytest.fail(f"{name} failed:\n{((e.stdout or '') + (e.stderr or ''))[-3000:]}")


def _retarget_tb(src: str, kernel: str) -> str:
    """The generated poly testbench, calling `kernel` instead of poly."""
    out = src.replace('#include "poly.hpp"', f'#include "{kernel}.hpp"')
    return re.sub(r"^(\s*)poly\(s_in,", rf"\1{kernel}(s_in,", out, flags=re.M)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")
    root = tmp_path_factory.mktemp("body_only")
    cfg = BuildConfig(root_dir=root)
    write_legacy_inputs(root, nsamp=100)
    HlsGenIncludeStep(name="gen_include").run(cfg)
    (root / "gen").mkdir(exist_ok=True)

    # The reference: the original extracted poly kernel and its hook.
    for name, text in kernel_files_to_str(PolyAccel, output_dir="gen", impl_dir=".").items():
        if name.endswith((".hpp", ".cpp")):
            (root / "gen" / name).write_text(text, encoding="utf-8")
    shutil.copy(HERE.parent / "fixtures" / "poly_extracted" / "poly_evaluate_impl.tpp",
                root / "poly_evaluate_impl.tpp")
    # The body-only kernel and its hand-written body.
    for name, text in kernel_files_to_str(PolyBody, output_dir="gen", impl_dir=".").items():
        if name.endswith((".hpp", ".cpp")):
            (root / "gen" / name).write_text(text, encoding="utf-8")
    shutil.copy(FIX / "polyb_run_impl.tpp", root / "polyb_run_impl.tpp")
    shutil.copy(FIX / "polyb_tb_err.cpp", root / "gen" / "polyb_tb_err.cpp")

    # One generated testbench, run against both kernels on separate copies of the inputs.
    HlsCodegenStep(name="gen_tb", comp_class=PolyTBHls, source_artifact="x",
                   output_dir="gen", is_testbench=True).run(cfg)
    tb = (root / "gen" / "poly_tb.cpp").read_text(encoding="utf-8")
    (root / "gen" / "polyb_tb.cpp").write_text(_retarget_tb(tb, "polyb"), encoding="utf-8")
    for d in ("data_ref", "data_body", "data_err"):
        shutil.copytree(root / "data", root / d)

    logs = {
        "ref": _vitis(root, "ref.tcl", _tcl("poly", "gen/poly.cpp", "gen/poly_tb.cpp", "data_ref", False)),
        "body": _vitis(root, "body.tcl", _tcl("polyb", "gen/polyb.cpp", "gen/polyb_tb.cpp", "data_body", True)),
        "err": _vitis(root, "err.tcl", _tcl("polyb", "gen/polyb.cpp", "gen/polyb_tb_err.cpp", "data_err", False)),
    }
    for k, v in logs.items():
        (root / f"{k}.log").write_text(v, encoding="utf-8")
    return root


def test_csim_is_byte_identical_to_the_extracted_kernel(built: Path) -> None:
    for f in ("resp_hdr_data.bin", "samp_out_data.bin", "regmap_status.json"):
        ref = (built / "data_ref" / f).read_bytes()
        body = (built / "data_body" / f).read_bytes()
        assert body == ref, f"{f} differs between the body-only and the extracted kernel"


def test_csynth_succeeds(built: Path) -> None:
    assert "GATE_CSYNTH_DONE" in (built / "body.log").read_text(encoding="utf-8")


def test_register_offsets_match_the_waveflow_regmap(built: Path) -> None:
    xml = next(built.glob("polyb_polyb_tb_proj/solution1/syn/report/csynth.xml")).read_text(
        encoding="utf-8")
    vitis = {n: int(o, 16) for o, n in
             re.findall(r'<register offset="(0x[0-9a-fA-F]+)" name="(\w+)"', xml)}
    vitis.update({n: int(o) for n, o in re.findall(r'memorieName="(\w+)" offset="(\d+)"', xml)})
    rm = elaborate(PolyAccel).regmap
    for name in (*STATUS, "coeffs"):
        assert vitis.get(name) == rm.offset_of(name), (
            f"{name}: Vitis put it at {vitis.get(name)}, the VitisRegMap says "
            f"{rm.offset_of(name)}")


def test_error_path_writes_status_through_the_references(built: Path) -> None:
    log = (built / "err.log").read_text(encoding="utf-8")
    csim = next(built.glob("polyb_polyb_tb_err_proj/solution1/csim/report/*.log")).read_text(
        encoding="utf-8")
    assert "GATE_ERRPATH_PASS" in csim + log, csim[-2000:]
