"""The vendor cores of a csynth'd kernel, for an XSI run (``waveflow.build.hls_ip``), without a
toolchain: what is found, what is cached, what reaches the workspace.  Generating the cores and running
them is the stream_kernel XSI gate (``tests/examples/test_stream_kernel.py``, ``-m xsi``) -- the first
system-flow kernel with float operators."""
from __future__ import annotations

import json
from pathlib import Path

from waveflow.build.hls_ip import csynth_part, generate_hls_ips, ip_tcls
from waveflow.build.xsi_workspace import XsiWorkspace

_TCL = ("create_ip -name floating_point -version $fpo_ver -vendor xilinx.com -library ip "
        "-module_name k_fmul_ip\n")
_WRAPPER = ("LIBRARY ieee;\nUSE ieee.std_logic_1164.ALL;\n\nLIBRARY floating_point_v7_1_20;\n"
            "USE floating_point_v7_1_20.floating_point_v7_1_20;\n\nENTITY k_fmul_ip IS\nEND;\n")


def _solution(tmp: Path) -> Path:
    sol = tmp / "k_proj" / "solution1"
    v = sol / "syn" / "verilog"
    v.mkdir(parents=True)
    (v / "k.v").write_text("module k; endmodule\n", encoding="utf-8")
    (v / "k_fmul_ip.tcl").write_text(_TCL, encoding="utf-8")
    (sol / "syn" / "report").mkdir()
    (sol / "syn" / "report" / "csynth.xml").write_text(
        "<profile><UserAssignments><Part>xc7z020-clg484-1</Part></UserAssignments></profile>",
        encoding="utf-8")
    return sol


def test_the_cores_are_the_ip_tcls_and_the_part_is_csynths(tmp_path):
    sol = _solution(tmp_path)
    assert [p.name for p in ip_tcls(sol / "syn" / "verilog")] == ["k_fmul_ip.tcl"]
    assert csynth_part(sol) == "xc7z020-clg484-1"
    assert csynth_part(tmp_path / "nowhere") is None


def test_a_generated_set_is_reused_with_its_libraries(tmp_path):
    """A stamped set is collected without Vivado (``vivado`` would fail if called): the wrappers, and
    the precompiled libraries they name -- never ieee/std/work."""
    tcl = ip_tcls(_solution(tmp_path) / "syn" / "verilog")
    first = tmp_path / "ip"
    import hashlib
    h = hashlib.sha256(b"xc7z020-clg484-1")
    h.update(_TCL.encode())
    work = first / f"hls_ip_{h.hexdigest()[:12]}"
    sim = work / "ip" / "k_fmul_ip" / "sim"
    sim.mkdir(parents=True)
    (sim / "k_fmul_ip.vhd").write_text(_WRAPPER, encoding="utf-8")
    (work / "done.json").write_text(json.dumps({}), encoding="utf-8")
    ip = generate_hls_ips(tcl, first, part="xc7z020-clg484-1", vivado="no-such-vivado")
    assert [p.name for p in ip.vhdl_files] == ["k_fmul_ip.vhd"]
    assert ip.libs == ["floating_point_v7_1_20"]


def test_the_workspace_lists_cores_only_when_there_are_some(tmp_path):
    ws = XsiWorkspace(tmp_path / "xsi", top="k")
    ws.prepare(rtl_files=["k.v"], tb_name="k_tb", tb_cpp="int main(){}\n",
               vhdl_files=[tmp_path / "core.vhd"], xelab_libs=["floating_point_v7_1_20"])
    assert (ws.work_dir / "vhdl_k.f").read_text(encoding="utf-8").strip().endswith("core.vhd")
    assert (ws.work_dir / "xelab_k.f").read_text(encoding="utf-8") == "-L floating_point_v7_1_20\n"
    ws.prepare(rtl_files=["k.v"], tb_name="k_tb", tb_cpp="int main(){}\n")
    assert not (ws.work_dir / "vhdl_k.f").exists() and not (ws.work_dir / "xelab_k.f").exists()
