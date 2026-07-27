"""The Vitis half of the ``add_state`` gate: the emitted ``static`` state array synthesizes.

Unit tests prove the generator emits the intended text.  Only Vitis can answer whether that text
is *accepted* — whether a ``static`` array declared inside an ``ap_ctrl_chain`` top and passed to
a hook synthesizes, and what it becomes.  ``plans/add_state.md`` flags the reset semantics of
initialized statics as verify-empirically; this is where that gets checked rather than assumed.

A failed csynth is a real failure; we skip only when Vitis is not installed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from waveflow.build.hwgen import kernel_files_to_str
from waveflow.toolchain import toolchain

POLY_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"

_TCL = """\
open_project -reset waveflow_poly_state_proj
set_top poly_state
add_files poly_state.cpp -cflags "-I."
open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: C-synthesis failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: poly_state csynth passed."
"""


def _stage(tmp_path: Path) -> Path:
    """Emit the state-backed poly kernel beside FRESHLY GENERATED headers + poly's hook body.

    The headers are generated, not copied from ``examples/stream_inband/include``: those
    committed copies are stale relative to the committed hook body (they predate the
    ``*_lane`` array-utils API it calls), so copying them fails the build for reasons that have
    nothing to do with ``add_state``.  This mirrors what ``poly_build.HlsGenIncludeStep`` does.
    """
    from examples.stream_inband.poly import SCHEMA_CLASSES, WORD_BW_SUPPORTED, Float32
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.arrayutils import ArrayUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    from tests.hw.state_poly_fixture import PolyStateAccel

    for name, content in kernel_files_to_str(
        PolyStateAccel, output_dir=".", impl_dir=".",
    ).items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    cfg = BuildConfig(root_dir=tmp_path)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir="include"))
    dag.add(ArrayUtilsStep(Float32, WORD_BW_SUPPORTED))
    results = dag.run(cfg)
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"header generation failed: {failed}"
    # The hook body is poly's, verbatim except for the namespace — the point of the retrofit is
    # that add_state changed where `coeffs` LIVES, not what the hook does with it.
    impl = (POLY_DIR / "poly_evaluate_impl.tpp").read_text(encoding="utf-8")
    impl = impl.replace("namespace poly_impl", "namespace poly_state_impl")
    impl = re.sub(r"\bpoly_impl::", "poly_state_impl::", impl)
    (tmp_path / "poly_state_evaluate_impl.tpp").write_text(impl, encoding="utf-8")

    (tmp_path / "run.tcl").write_text(_TCL, encoding="utf-8")
    return tmp_path


@pytest.mark.vitis
def test_state_array_csynthesizes(tmp_path):
    """The generated ``static float coeffs[4];`` top synthesizes under Vitis HLS."""
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    root = _stage(tmp_path)
    try:
        toolchain.run_vitis_hls(root / "run.tcl", work_dir=root)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"csynth of the add_state kernel failed\nrc={exc.returncode}\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        )
    report = root / "waveflow_poly_state_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    assert report.exists(), "csynth reported success but produced no report"


@pytest.mark.vitis
def test_state_array_becomes_a_memory_not_a_port(tmp_path):
    """The state array is internal storage: it must NOT appear as a top-level interface port.

    This is the observable difference from the regmap version, checked against the tool's own
    report rather than against our emitted text — the emitter could be right about the source and
    still be wrong about what Vitis makes of it.
    """
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    root = _stage(tmp_path)
    try:
        toolchain.run_vitis_hls(root / "run.tcl", work_dir=root)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"csynth failed\nrc={exc.returncode}\nstdout:\n{exc.stdout}")
    report = (
        root / "waveflow_poly_state_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    ).read_text(encoding="utf-8", errors="replace")
    # <RtlPorts><name>..</name><Object>..</Object>..: `Object` is the C-level source the port
    # came from, which is what "did coeffs become an interface?" actually asks.
    ports = re.findall(r"<RtlPorts>\s*<name>([^<]+)</name>\s*<Object>([^<]*)</Object>", report)
    # Guard against a vacuous pass: if the parse breaks, an empty list would satisfy any
    # "not in" assertion below and the test would silently stop testing anything.
    assert ports, "parsed no RTL ports from csynth.xml — the report format changed"
    assert any(p.startswith("s_axi_control") for p, _ in ports), (
        f"expected the s_axilite control ports in {[p for p, _ in ports]}"
    )
    offenders = [p for p, obj in ports if obj == "coeffs" or p.startswith("coeffs")]
    assert not offenders, (
        f"'coeffs' surfaced as top-level port(s) {offenders!r} — declared state must be "
        f"internal storage, not an interface"
    )


_TASK_TCL = """\
open_project -reset waveflow_accum_proj
set_top accum_top
add_files accum_top.cpp -cflags "-I."
open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: C-synthesis failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: accum csynth passed."
"""

#: A free-running ap_ctrl_none top instantiating the generated stateful task, plus the hook body
#: the generator leaves as a TODO stub.  This is the Stage-2 shape: the static lives in the TASK.
_TASK_TOP = """\
#include "hls_task.h"
#include "accum_task.h"

namespace accum_impl {
Vec accumulate(Vec x, float total[4]) {
#pragma HLS INLINE
    Vec y;
    for (int i = 0; i < 4; ++i) {
#pragma HLS UNROLL
        total[i] = total[i] + x.data[i];
        y.data[i] = total[i];
    }
    return y;
}
}

void accum_top(hls::stream<ap_uint<32> >& x_in, hls::stream<ap_uint<32> >& y_out) {
#pragma HLS INTERFACE axis port=x_in
#pragma HLS INTERFACE axis port=y_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t0(accum_task, x_in, y_out);
}
"""


@pytest.mark.vitis
def test_task_body_static_csynthesizes(tmp_path):
    """Stage 2's csynth half: a ``static`` inside a free-running ``hls::task`` body synthesizes.

    Distinct from the ap_ctrl_chain case above — a task has no "before the loop", so this is the
    shape where the static IS the persistent storage the runtime's re-firings carry forward.
    (What csynth still cannot answer is whether it persists in RTL; that is the XSI gate.)
    """
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.hwgen import task_files_to_str
    from waveflow.build.streamutils import StreamUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    from tests.hw.test_add_state import AccArray, Vec

    for name, content in task_files_to_str(
        __import__("tests.hw.test_add_state", fromlist=["Accum"]).Accum,
    ).items():
        if name.endswith("_impl.cpp"):
            continue                      # the TODO stub; the real body is in _TASK_TOP
        (tmp_path / name).write_text(content, encoding="utf-8")
    (tmp_path / "accum_top.cpp").write_text(_TASK_TOP, encoding="utf-8")
    (tmp_path / "run.tcl").write_text(_TASK_TCL, encoding="utf-8")

    cfg = BuildConfig(root_dir=tmp_path)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="."))
    # Both the payload AND the state array need headers: the state arg is annotated HwState, so
    # _collect_schemas resolves it to the wrapped class and includes that class's header.
    for cls in (Vec, AccArray):
        dag.add(DataSchemaStep(cls, word_bw_supported=[32], include_dir="."))
    results = dag.run(cfg)
    assert all(r.success for r in results.values()), f"header generation failed: {results}"

    try:
        toolchain.run_vitis_hls(tmp_path / "run.tcl", work_dir=tmp_path)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"csynth of the stateful task body failed\nrc={exc.returncode}\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        )
    rpt = tmp_path / "waveflow_accum_proj" / "solution1" / "syn" / "report" / "csynth.xml"
    assert rpt.exists(), "csynth reported success but produced no report"
