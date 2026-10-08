"""plans/xsi_system_top.md -- generated XSI system tops, and the host as a hooked module.

Stage 1 (the duals): a host's ports have BFM duals.  ``BFM_DUALS["mm_slave"]`` is ``AxiMmMaster`` (the
testbench masters a system top's crossbar SI) and an interrupt output is answered by ``IrqPin``, so
``check(host, "xsi_bfm_model")`` resolves mm_fir's host -- its bus master and its three interrupt sinks.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from waveflow.build.codegen_check import check
from waveflow.build.composite_gen import (
    BFM_DUALS,
    BfmModel,
    bfm_dual_class,
    render_tb_harness,
    tb_top_spec,
    xsi_model_classes,
)
from waveflow.hw.codegen_targets import XSI_BFM_MODEL
from waveflow.simulation.stream_tb import StreamSink

from examples.mm_fir.mm_fir import FirHost, MmFirSystem

HOST_PORTS = ("m", "irq_qin", "irq_qout", "irq_qresp")


@dataclass
class _FirHostWithModels(FirHost):
    """mm_fir's host, declaring the framework models its ports take (Stage 1 only: Stage 4 gives
    FirHost its own model, in a header beside the example)."""

    def bfm_model(self):
        return (BfmModel("AxiMmMaster", ports=("m",)),
                BfmModel("IrqPin", ports=("irq_qin",)),
                BfmModel("IrqPin", ports=("irq_qout",)),
                BfmModel("IrqPin", ports=("irq_qresp",)))


def test_the_host_duals_are_filled():
    assert bfm_dual_class("mm_slave", None) == "AxiMmMaster"
    assert bfm_dual_class("irq_out", None) == "IrqPin"
    assert BFM_DUALS["mm_slave"].model in xsi_model_classes()
    assert "IrqPin" in xsi_model_classes()


def test_check_resolves_mm_fir_host_ports():
    """The Stage 1 gate: the host of a wired mm_fir system resolves under the strictest cut."""
    sysm = MmFirSystem(x=[0], plan=[(0, [1])])
    host = sysm.host
    assert sorted(a for a in vars(host) if any(host.endpoints[k] is getattr(host, a)
                                               for k in host.endpoints)) == sorted(HOST_PORTS)
    model_host = _FirHostWithModels(name="host2", sim=sysm.sim, x=[0], plan=[(0, [1])])
    assert check(model_host, XSI_BFM_MODEL) == (True, None)


def test_an_uncovered_irq_is_named():
    @dataclass
    class _NoIrq(FirHost):
        def bfm_model(self):
            return BfmModel("AxiMmMaster", ports=("m",))

    sysm = MmFirSystem(x=[0], plan=[(0, [1])])
    ok, msg = check(_NoIrq(name="h", sim=sysm.sim, x=[0], plan=[(0, [1])]), XSI_BFM_MODEL)
    assert ok is False and "irq_qin" in msg


# ---------------------------------------------------------------------------------------------
# Stage 2: BfmModel(header=...) -- a model class beside the example, not in the framework library.
# ---------------------------------------------------------------------------------------------


_XSI_SRC = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"
_GXX = shutil.which("g++")


@dataclass
class _LocalSink(StreamSink):
    """A sink whose C++ twin is ``xsi_local/counting_sink.h``, beside this file."""

    def bfm_model(self):
        return BfmModel("CountingSink", ports=("stream_ep",), header="xsi_local/counting_sink.h")


@dataclass
class _LocalTypo(StreamSink):
    """Names a class the local header does not define -- but the framework library does."""

    def bfm_model(self):
        return BfmModel("AxisSlave", ports=("stream_ep",), header="xsi_local/counting_sink.h")


def _mem_copy_tb(sink_cls):
    from examples.mem_copy.mem_copy_sim import MemCopyTB
    from waveflow.simulation.simulation import Simulation

    tb = MemCopyTB(name="tb", sim=Simulation(), mem_dwidth=64)
    tb.done_sink.__class__ = sink_cls          # same instance, same wiring: only the hook differs
    return tb


def test_a_local_model_resolves_against_its_own_header():
    tb = _mem_copy_tb(_LocalSink)
    assert check(tb.done_sink, XSI_BFM_MODEL) == (True, None)


def test_a_local_model_is_not_looked_up_in_the_library():
    """`AxisSlave` exists in xsi_bfm.h; naming it with a local header must still fail, against the
    header that was named."""
    tb = _mem_copy_tb(_LocalTypo)
    ok, msg = check(tb.done_sink, XSI_BFM_MODEL)
    assert ok is False and "counting_sink.h" in msg and "CountingSink" in msg


def test_a_missing_local_header_is_named():
    @dataclass
    class _Missing(StreamSink):
        def bfm_model(self):
            return BfmModel("CountingSink", ports=("stream_ep",), header="xsi_local/nope.h")

    tb = _mem_copy_tb(_Missing)
    ok, msg = check(tb.done_sink, XSI_BFM_MODEL)
    assert ok is False and "nope.h" in msg and "no such file" in msg


def test_the_harness_includes_and_constructs_the_local_model():
    spec = tb_top_spec(_mem_copy_tb(_LocalSink))
    (m,) = [m for m in spec.models if m.name == "s_done"]
    assert m.cls == "CountingSink"
    assert [Path(h).name for h in spec.local_headers] == ["counting_sink.h"]
    assert Path(spec.local_headers[0]).is_file()
    assert '#include "counting_sink.h"' in render_tb_harness(spec)


def _vivado_xsim_include():
    for root in (Path("C:/Xilinx"), Path("/opt/Xilinx")):
        for base in sorted(root.glob("*/Vivado/data/xsim/include"), reverse=True):
            if base.is_dir():
                return base
    return None


@pytest.mark.skipif(_GXX is None, reason="g++ not on PATH")
def test_the_harness_with_a_local_model_compiles(tmp_path):
    """The Stage 2 gate: the generated harness, with the local model, is well-formed C++ (syntax-only:
    xsi_bfm.h reaches Vivado's xsi.h)."""
    from waveflow.build.composite_gen import composite_top_spec, render_ports_h

    inc = _vivado_xsim_include()
    if inc is None:
        pytest.skip("no Vivado data/xsim/include found -- cannot compile a harness (xsi.h)")
    tb = _mem_copy_tb(_LocalSink)
    spec = tb_top_spec(tb)
    (tmp_path / "mem_copy_ports.h").write_text(render_ports_h(composite_top_spec(tb.dut, width=64)),
                                               encoding="utf-8")
    (tmp_path / "mem_copy_tb_harness.h").write_text(render_tb_harness(spec), encoding="utf-8")
    for h in spec.local_headers:
        shutil.copy(h, tmp_path)
    (tmp_path / "main.cpp").write_text(
        '#include "mem_copy_tb_harness.h"\n'
        'int main() { mem_copy_tb::Harness h("x.wdb"); h.run(10);\n'
        '             long n = h.s_done.beats; h.close(); return (int)n; }\n', encoding="utf-8")
    r = subprocess.run([_GXX, "-std=c++17", "-fsyntax-only", f"-I{_XSI_SRC}", f"-I{inc}",
                        f"-I{tmp_path}", str(tmp_path / "main.cpp")],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"the harness with a local model does not compile:\n{r.stderr[-4000:]}"


# ---------------------------------------------------------------------------------------------
# Stage 4: the host is a hooked module -- mm_fir's FirHost and its C++ twin, FirHostModel.
# ---------------------------------------------------------------------------------------------

def test_fir_host_resolves_its_own_model():
    """FirHost names FirHostModel, in mm_fir_host.h beside it -- and the class really is there."""
    sysm = MmFirSystem(x=[0], plan=[(0, [1])])
    assert check(sysm.host, XSI_BFM_MODEL) == (True, None)
    bm = sysm.host.bfm_model()
    assert (bm.cls, bm.header, bm.ports) == ("FirHostModel", "mm_fir_host.h", HOST_PORTS)


def test_the_scenario_file_drives_the_host_exactly_as_in_memory(tmp_path):
    """The same scenario, from memory and from the bundle the C++ host reads: the same outputs, and
    every endpoint's trace the same bytes."""
    import numpy as np

    from examples.mm_fir.mm_fir import HOST_ENDPOINTS
    from examples.mm_fir.mm_fir_xsi import PKT, PLAN, scenario_x

    def run(scenario: str, traces):
        sysm = MmFirSystem(x=list(scenario_x()), plan=PLAN, pkt=PKT)
        sysm.host.scenario = scenario
        sysm.host.trace_dir = str(traces)
        return sysm.run(), sysm

    y0, sysm = run("", tmp_path / "mem")
    sysm.host.write_scenario(tmp_path / "scenario")
    y1, _ = run(str(tmp_path / "scenario"), tmp_path / "file")
    assert np.array_equal(y0, y1)
    for ep in HOST_ENDPOINTS:
        for f in ("words.bin", "bounds.bin", "meta.json"):
            assert (tmp_path / "mem" / ep / f).read_bytes() == (tmp_path / "file" / ep / f).read_bytes()
    assert (tmp_path / "mem" / "qout" / "words.bin").stat().st_size == 8 * len(y0)


def test_the_system_harness_binds_the_host_to_the_top():
    from examples.mm_fir.mm_fir_xsi import system, system_spec
    from waveflow.build.system_top import render_system_tb, system_tb_spec

    sysm = system("one_front")
    spec = system_spec("one_front", sysm)
    tb = system_tb_spec(spec, sysm.xbar, [sysm.host])
    (m,) = tb.models
    assert m.cls == "FirHostModel" and tb.stop_on == "host"
    assert m.binds == tuple(x for p in ("s0_axi", "irq_qin", "irq_qout", "irq_qresp")
                            for x in ("sim.dut()", f"mm_fir_top_ports::{p}"))
    main, files = render_system_tb(spec, tb)
    assert "h.run_until(" in main and "mm_fir_host.h" in files
    assert "long run_until(long n_max)" in files["mm_fir_top_tb_harness.h"]


# ---------------------------------------------------------------------------------------------
# Stage 5: markov's host -- two kernels, four masters, a credit link; the host also reads x back.
# ---------------------------------------------------------------------------------------------

def test_markov_host_resolves_its_own_model():
    from examples.markov.markov_xsi import system
    host = system().host
    assert check(host, XSI_BFM_MODEL) == (True, None)
    bm = host.bfm_model()
    assert (bm.cls, bm.header, bm.ports) == ("MarkovHostModel", "markov_host.h",
                                            ("m", "irq_qcmd", "irq_qresp"))
    # No arguments: the host's settings travel as DynParams (max_in_flight), and the C++ reads its
    # responses typed (qresp.get<MkvResp>()) -- no bit position is handed over (host_runtime S3/S4).
    assert bm.extra_args == ()


def test_markov_scenario_file_drives_the_host_exactly_as_in_memory(tmp_path):
    from examples.markov.markov import HOST_ENDPOINTS
    from examples.markov.markov_xsi import system

    def run(scenario: str, traces):
        sysm = system()
        sysm.host.scenario = scenario
        sysm.host.trace_dir = str(traces)
        return sysm.run(), sysm

    r0, sysm = run("", tmp_path / "mem")
    sysm.host.write_scenario(tmp_path / "scenario")
    r1, _ = run(str(tmp_path / "scenario"), tmp_path / "file")
    assert sorted(r0) == sorted(r1) and all((r0[j]["x"] == r1[j]["x"]).all() for j in r0)
    for ep in HOST_ENDPOINTS:
        for f in ("words.bin", "bounds.bin", "meta.json"):
            assert (tmp_path / "mem" / ep / f).read_bytes() == (tmp_path / "file" / ep / f).read_bytes()
