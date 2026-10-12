"""``waveflow/build/sw_host_gen.py`` -- a software host's generated ``<Host>_endpoints.h``
(plans/host_runtime.md S2).

The RTL gate (tests/examples/test_mm_fir_xsi.py: 618 / 611, traces identical) runs the generated
header for real; these are the fast checks: what is read off the wired host, the ports, the refusals,
and that the whole generated testbench is well-formed C++ under the compilers run.bat may use.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from waveflow.build.hwcodegen import LoweringError
from waveflow.build.sw_host_gen import host_layout, host_ports, render_host_endpoints_h

XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"


def _fir(topology="per_view"):
    from examples.mm_fir.mm_fir_build import system
    return system(topology)


def test_the_fir_host_layout_is_read_off_the_wiring():
    sysm = _fir()
    lay = host_layout(sysm.host)
    assert lay["bus"] == "m"
    assert lay["irqs"] == ["irq_qin", "irq_qout", "irq_qresp"]
    assert [(a, k, irq) for a, k, _src, irq in lay["endpoints"]] == [
        ("cfg", "RegCfg", None), ("qin", "QueueWriter", "irq_qin"), ("qout", "QueueReader", "irq_qout"),
        ("qresp", "QueueReader", "irq_qresp"), ("status", "StatusReader", None)]
    assert host_ports(sysm.host) == ("m", "irq_qin", "irq_qout", "irq_qresp")


def test_the_header_names_views_by_address_never_by_hand():
    sysm = _fir()
    h = render_host_endpoints_h(sysm.host)
    assert "class FirHost_endpoints : public SwHostModel" in h
    q = sysm.host.qin.interface.view
    assert f'qin(sched_, bus_, MmView{{"qin", MmKind::QueueIn, 0x{q.base:x}ull' in h
    assert ", 8, &irq_qin)" in h                      # poll period and interrupt, from Python
    assert 'traced("status", &status);' in h


def test_markov_host_layout_includes_its_memory_reads():
    from examples.markov.markov_build import system
    lay = host_layout(system().host)
    assert [(a, k) for a, k, *_ in lay["endpoints"]] == [
        ("qcmd", "QueueWriter"), ("qresp", "QueueReader"), ("mem_reader", "BusRw")]


def test_a_host_with_no_bus_master_is_refused():
    from waveflow.simulation.simulation import Simulation
    from waveflow.sw import SwHost

    with pytest.raises(LoweringError, match="exactly one bus master"):
        host_layout(SwHost(name="h", sim=Simulation()))


def _vivado_xsim_include():
    for base in sorted(Path("C:/Xilinx").glob("*/Vivado/data/xsim/include"), reverse=True):
        return base
    for base in sorted(Path("/opt/Xilinx").glob("*/Vivado/data/xsim/include"), reverse=True):
        return base
    return None


def _compilers():
    out = []
    for v in ("6.2.0", "10.0.0"):
        for root in sorted(Path("C:/Xilinx").glob(f"*/Vivado/tps/mingw/{v}/win64.o/nt/bin")):
            if (root / "g++.exe").is_file():
                out.append((v, root / "g++.exe"))
                break
    if not out and shutil.which("g++"):
        out.append(("path", Path(shutil.which("g++"))))
    return out


@pytest.mark.parametrize("label,gxx", _compilers() or [("none", None)], ids=lambda x: str(x))
def test_the_generated_fir_testbench_compiles(label, gxx, tmp_path):
    """The whole generated testbench -- harness, ports header, FirHost_endpoints.h, the user's
    mm_fir_host.h on the fiber runtime -- is well-formed C++ (syntax-only: xsi.h is Vivado's)."""
    import os

    from examples.mm_fir.mm_fir_build import SYSTEM_TOP, XBAR_NAMES, system
    from waveflow.build.system_top import render_system_tb, system_tb_spec, system_top_spec

    inc = _vivado_xsim_include()
    if gxx is None or inc is None:
        pytest.skip("needs g++ and Vivado's data/xsim/include")
    sysm = system("one_front")
    spec = system_top_spec(sysm.xbar, [sysm.fir], top=SYSTEM_TOP, xbar_name=XBAR_NAMES["one_front"])
    sysm.host.scenario, sysm.host.trace_dir = "scenario", "traces"
    main, files = render_system_tb(spec, system_tb_spec(spec, sysm.xbar, [sysm.host]))
    (tmp_path / "main.cpp").write_text(main, encoding="utf-8")
    for name, text in files.items():               # no address headers: the views carry addresses
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = dict(os.environ, PATH=f"{gxx.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    r = subprocess.run([str(gxx), "-std=c++14", "-fsyntax-only", "-Wall", f"-I{XSI_SRC}", f"-I{inc}",
                        f"-I{tmp_path}", str(tmp_path / "main.cpp")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "warning" not in r.stderr, r.stderr[-4000:]
