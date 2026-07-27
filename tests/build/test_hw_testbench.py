"""Tests for the ``HwTestbench`` class and testbench-mode extractor.

Phase 14 of the HwModule codegen project introduces a separate codegen
source for testbench C++.  Phase 1 (this file) covers the wiring: the new
``HwTestbench`` class, its ``main()`` placeholder, and the
``extract_kernel`` routing that dispatches testbench subclasses through
``extract_testbench`` / the ``is_testbench=True`` extractor mode.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.build.hwcodegen import (
    HwStmtExtractor,
    extract_kernel,
    extract_testbench,
)
from waveflow.hw.hw_testbench import HwTestbench, SeqTB
from waveflow.hw.hwstmt import SeqStmt


pytestmark = pytest.mark.phase1


# ---------------------------------------------------------------------------
# Phase 1 — class + routing
# ---------------------------------------------------------------------------

def test_seqtb_is_a_named_object_not_a_hwcomponent():
    """``SeqTB`` is a sim-less :class:`NamedObject` — a program that drives a kernel, **not** a
    ``HwModule``/``SimObj`` hardware object.  ``HwTestbench`` is a deprecated alias of ``SeqTB``."""
    from waveflow.hw.hw_module import HwModule
    from waveflow.named import NamedObject
    assert issubclass(SeqTB, NamedObject)
    assert not issubclass(SeqTB, HwModule)
    assert HwTestbench is SeqTB


def test_hw_testbench_marker_is_set():
    """The codegen routing dispatches on the ``_is_testbench`` class
    marker.  Subclasses inherit ``True``; ``HwModule`` proper does not
    have the marker set."""
    from waveflow.hw.hw_module import HwModule
    assert getattr(HwTestbench, '_is_testbench', False) is True
    assert getattr(HwModule, '_is_testbench', False) is False


def test_base_main_raises_not_implemented():
    """The base-class ``main()`` is a placeholder that fails fast when a
    subclass forgets to override it."""
    tb = HwTestbench(name='unused')
    with pytest.raises(NotImplementedError, match='main'):
        tb.main()


@dataclass
class _EmptyTB(HwTestbench):
    """Trivial subclass — body is docstring-only, no real testbench logic
    yet.  Phase 3+ exercises real extraction; Phase 1 just confirms the
    routing through the extractor doesn't crash on a minimal body."""

    def main(self) -> None:
        """Phase 1 placeholder body."""


def test_extract_testbench_routes_through_main():
    """``extract_testbench`` reads ``comp.main`` (not ``run_proc``) and
    produces a tree without raising on the trivial body."""
    tb = _EmptyTB(name='tb')
    tree = extract_testbench(tb)
    assert isinstance(tree, SeqStmt)
    assert tree.stmts == []


def test_extract_kernel_dispatches_testbench_subclasses():
    """The legacy ``extract_kernel`` entry point auto-routes testbench
    subclasses through ``extract_testbench`` — callers don't need to
    branch on the marker."""
    tb = _EmptyTB(name='tb')
    tree = extract_kernel(tb)
    assert isinstance(tree, SeqStmt)


def test_extractor_carries_is_testbench_flag():
    """The mode flag is plumbed through; ``HwStmtExtractor`` stashes it
    so Phase 3/4 emitter logic can branch on the extractor's mode."""
    tb = _EmptyTB(name='tb')
    ext = HwStmtExtractor(tb, method_name='main', is_testbench=True)
    assert ext._is_testbench is True
    # Default is False — preserves backwards compat for kernel-mode callers.
    kernel_ext = HwStmtExtractor(tb, method_name='main')
    assert kernel_ext._is_testbench is False


# ---------------------------------------------------------------------------
# Phase 2 — HlsCodegenStep testbench mode
# ---------------------------------------------------------------------------

from typing import ClassVar


@dataclass
class _PolyTbStub(HwTestbench):
    """Minimal testbench class with ``cpp_kernel_name = "poly"`` so the
    Phase-2 emitter writes ``gen/poly_tb.cpp`` (matching what Phase 6
    will plug into ``poly_build.py``).  The body stays a docstring
    placeholder — real extraction lands in Phase 3+."""

    cpp_kernel_name: ClassVar[str | None] = "poly"

    def main(self) -> None:
        """Phase-2 placeholder body."""


@pytest.mark.phase2
def test_hls_codegen_step_auto_detects_testbench_mode():
    from waveflow.build.hwcodegen_steps import HlsCodegenStep
    step = HlsCodegenStep(
        comp_class=_PolyTbStub,
        source_artifact="poly_source",
        output_dir="gen",
    )
    assert step._is_testbench is True
    # Kernel-mode component stays in kernel mode.
    from tests.hw.test_resolve import Demo
    kernel_step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="demo_src",
        output_dir="gen",
    )
    assert kernel_step._is_testbench is False


@pytest.mark.phase2
def test_hls_codegen_step_explicit_is_testbench_override():
    """``is_testbench=True`` forces TB mode even on a non-marker class."""
    from waveflow.build.hwcodegen_steps import HlsCodegenStep
    from tests.hw.test_resolve import Demo
    step = HlsCodegenStep(
        comp_class=Demo,
        source_artifact="x",
        output_dir="gen",
        is_testbench=True,
    )
    assert step._is_testbench is True


@pytest.mark.phase2
def test_hls_codegen_step_testbench_produces_single_tb_file():
    """In TB mode, ``produces`` is just ``{<kernel>_tb: <kernel>_tb.cpp}``."""
    from pathlib import Path
    from waveflow.build.hwcodegen_steps import HlsCodegenStep
    step = HlsCodegenStep(
        comp_class=_PolyTbStub,
        source_artifact="poly_source",
        output_dir="gen",
    )
    assert step.produces == {"poly_tb": Path("gen/poly_tb.cpp")}


@pytest.mark.phase2
def test_hls_codegen_step_run_emits_skeleton_tb_cpp(tmp_path):
    """``run()`` writes a compilable skeleton file in TB mode."""
    from waveflow.build.build import BuildConfig
    from waveflow.build.hwcodegen_steps import HlsCodegenStep
    step = HlsCodegenStep(
        comp_class=_PolyTbStub,
        source_artifact="poly_source",
        output_dir="gen",
    )
    artifacts = step.run(BuildConfig(root_dir=tmp_path))
    tb_path = tmp_path / "gen" / "poly_tb.cpp"
    assert artifacts == {"poly_tb": tb_path}
    body = tb_path.read_text(encoding="utf-8")
    # Skeleton must compile and reference the kernel header.
    assert '#include "poly.hpp"' in body
    assert "int main(int argc, char** argv)" in body
    assert "return 0;" in body


@pytest.mark.phase2
def test_tb_files_to_str_returns_single_file():
    from waveflow.build.hwgen import tb_files_to_str
    files = tb_files_to_str(_PolyTbStub, output_dir="gen")
    assert set(files) == {"poly_tb.cpp"}
    assert "int main(" in files["poly_tb.cpp"]


# ---------------------------------------------------------------------------
# Phase 3 — DUT binding + dut.run() lowering
# ---------------------------------------------------------------------------

from examples.stream_inband.poly import PolyAccel


@dataclass
class _PolyTBPhase3(HwTestbench):
    """Minimal Phase-3 fixture: bind a PolyAccel DUT and call run().

    Exercises the two IR nodes added in Phase 3 — ``DutBindStmt`` and
    ``KernelCallStmt`` — and the corresponding emitter logic in
    ``hwgen.tb_to_cpp``.  Subsequent phases extend the body with stream
    push/pop and file I/O against the same DUT binding.
    """

    cpp_kernel_name: ClassVar[str | None] = "poly"

    def main(self) -> None:
        dut = PolyAccel()
        dut.run()


@pytest.mark.phase3
def test_phase3_extractor_produces_dut_bind_and_kernel_call():
    """The TB-mode extractor turns ``dut = PolyAccel()`` + ``dut.run()``
    into a SeqStmt of [DutBindStmt, KernelCallStmt]."""
    from waveflow.build.hwcodegen import extract_testbench
    from waveflow.hw.hwstmt import DutBindStmt, KernelCallStmt
    tb = _PolyTBPhase3(name='tb')
    tree = extract_testbench(tb)
    assert isinstance(tree, SeqStmt)
    assert len(tree.stmts) == 2
    bind, call = tree.stmts
    assert isinstance(bind, DutBindStmt)
    assert bind.local_name == 'dut'
    assert bind.comp_class is PolyAccel
    assert bind.kwargs == {}
    assert isinstance(call, KernelCallStmt)
    assert call.local_name == 'dut'


@pytest.mark.phase3
def test_phase3_emits_stream_and_regmap_locals_and_kernel_call():
    """The TB emitter produces stream local decls, regmap field decls
    (scalars and the raw-array ``coeffs``), and the kernel-call line."""
    from waveflow.build.hwgen import tb_files_to_str
    files = tb_files_to_str(_PolyTBPhase3, output_dir="gen")
    body = files["poly_tb.cpp"]
    # Stream endpoints
    assert "hls::stream<streamutils::axi4s_word<32>> s_in;" in body
    assert "hls::stream<streamutils::axi4s_word<32>> m_out;" in body
    # Regmap scalars
    assert "ap_uint<1> halted = 0;" in body
    assert "ap_uint<8> error = 0;" in body
    assert "ap_uint<16> tx_id = 0;" in body
    # Raw-array regmap field
    assert "float coeffs[4] = {};" in body
    # Kernel call: arg order matches kernel_signature
    assert "poly(s_in, m_out, halted, error, tx_id, coeffs);" in body


@pytest.mark.phase3
def test_phase3_rejects_positional_dut_args():
    """DUT construction must use keyword arguments only — positional
    args are rejected at extraction time so the failure is surfaced
    before downstream emitter logic runs."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _BadPositionalTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "poly"

        def main(self) -> None:
            dut = PolyAccel("bad")  # noqa: F841
            dut.run()

    tb = _BadPositionalTB(name='tb')
    with pytest.raises(SynthesisError, match="keyword arguments only"):
        extract_testbench(tb)


@pytest.mark.phase3
def test_phase3_dut_run_with_args_is_rejected():
    """``dut.run(...)`` with positional args is rejected.  (The only accepted
    keyword is ``mem=<MemoryMod local>`` for m_axi kernels — see the AXI-MM
    codegen plan decision 9.)"""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _BadRunArgsTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "poly"

        def main(self) -> None:
            dut = PolyAccel()
            dut.run(42)

    tb = _BadRunArgsTB(name='tb')
    with pytest.raises(SynthesisError, match="dut.run\\(\\) takes no positional arguments"):
        extract_testbench(tb)


# ---------------------------------------------------------------------------
# Phase 4 — push/pop + file IO + status JSON
# ---------------------------------------------------------------------------

from examples.stream_inband.poly import CoeffArray, PolyCmdHdr, PolyRespHdr, Float32
from waveflow.hw.dataschema import DataArray


class SampArray(DataArray):
    """Buffer of up to 128 Float32 samples — used by the Phase-4 fixture
    to hold ``samp_in`` / ``samp_out`` arrays at compile-time size 128."""
    element_type = Float32
    static = True
    max_shape = (128,)
    cpp_storage = "raw"


@dataclass
class _PolyTBPhase4(HwTestbench):
    """End-to-end Phase-4 fixture mirroring the hand-written poly_tb.cpp."""

    cpp_kernel_name: ClassVar[str | None] = "poly"

    def main(self) -> None:
        dut = PolyAccel()

        dut.regmap.read_uint32_file_array(
            "coeffs", self.data_dir + "/coeffs.bin", count=4)

        data_hdr = PolyCmdHdr()
        data_hdr.read_uint32_file(self.data_dir + "/data_cmd_hdr.bin")

        samp_in = SampArray()
        samp_in.read_uint32_file_array(
            self.data_dir + "/samp_in_data.bin", count=data_hdr.nsamp)

        end_hdr = PolyCmdHdr()
        end_hdr.read_uint32_file(self.data_dir + "/end_cmd_hdr.bin")

        dut.s_in.push(data_hdr)
        dut.s_in.push_array(samp_in, count=data_hdr.nsamp)
        dut.s_in.push(end_hdr)

        dut.run()

        resp_hdr = PolyRespHdr()
        dut.m_out.pop(resp_hdr)

        samp_out = SampArray()
        dut.m_out.pop_array(samp_out, count=data_hdr.nsamp)

        resp_hdr.write_uint32_file(self.data_dir + "/resp_hdr_data.bin")
        samp_out.write_uint32_file_array(
            self.data_dir + "/samp_out_data.bin", count=data_hdr.nsamp)

        dut.regmap.write_status_json(
            self.data_dir + "/regmap_status.json",
            fields=["halted", "error", "tx_id"])


@pytest.mark.phase4
def test_phase4_emits_full_poly_testbench_body():
    """The Phase-4 emitter produces every pattern the hand-written
    poly_tb.cpp uses: schema locals, file I/O, stream push/pop,
    regmap file-read, kernel call, regmap status JSON."""
    from waveflow.build.hwgen import tb_files_to_str
    files = tb_files_to_str(_PolyTBPhase4, output_dir="gen")
    body = files["poly_tb.cpp"]

    # Include block
    assert '#include "poly.hpp"' in body
    assert '#include "include/streamutils_tb.h"' in body
    assert '#include "include/float32_array_utils_tb.h"' in body
    assert '#include "include/poly_cmd_hdr.h"' in body
    assert '#include "include/poly_resp_hdr.h"' in body

    # Local decls from Phase 3 (DUT bind)
    assert "hls::stream<streamutils::axi4s_word<32>> s_in;" in body
    assert "hls::stream<streamutils::axi4s_word<32>> m_out;" in body
    assert "float coeffs[4] = {};" in body

    # Schema-bound TB locals (one each)
    assert "PolyCmdHdr data_hdr;" in body
    assert "PolyCmdHdr end_hdr;" in body
    assert "PolyRespHdr resp_hdr;" in body
    assert "float samp_in[128] = {};" in body
    assert "float samp_out[128] = {};" in body

    # File reads (coeffs into regmap, headers, samples)
    assert ('float32_array_utils::read_uint32_file_array(coeffs, '
            '(data_dir + std::string("/coeffs.bin")).c_str(), 4);') in body
    assert ('streamutils::read_uint32_file(data_hdr, '
            '(data_dir + std::string("/data_cmd_hdr.bin")).c_str());') in body
    assert ('float32_array_utils::read_uint32_file_array(samp_in, '
            '(data_dir + std::string("/samp_in_data.bin")).c_str(), '
            'data_hdr.nsamp);') in body
    assert ('streamutils::read_uint32_file(end_hdr, '
            '(data_dir + std::string("/end_cmd_hdr.bin")).c_str());') in body

    # Stream pushes
    assert "data_hdr.write_axi4_stream<32>(s_in, true);" in body
    assert ("float32_array_utils::write_axi4_stream<32>(s_in, samp_in, "
            "true, data_hdr.nsamp);") in body
    assert "end_hdr.write_axi4_stream<32>(s_in, true);" in body

    # Kernel call
    assert "poly(s_in, m_out, halted, error, tx_id, coeffs);" in body

    # Stream pops
    assert "streamutils::tlast_status _tlast_resp_hdr = " in body
    assert "resp_hdr.read_axi4_stream<32>(m_out, _tlast_resp_hdr);" in body
    assert "streamutils::tlast_status _tlast_samp_out = " in body
    assert ("float32_array_utils::read_axi4_stream<32>(m_out, samp_out, "
            "_tlast_samp_out, data_hdr.nsamp);") in body

    # File writes
    assert ('streamutils::write_uint32_file(resp_hdr, '
            '(data_dir + std::string("/resp_hdr_data.bin")).c_str());') in body
    assert ('float32_array_utils::write_uint32_file_array(samp_out, '
            '(data_dir + std::string("/samp_out_data.bin")).c_str(), '
            'data_hdr.nsamp);') in body

    # Status JSON block
    assert "std::ofstream _status_ofs" in body
    assert r'\"halted\": " << (int)halted' in body
    assert r'\"error\": " << (int)error' in body
    assert r'\"tx_id\": " << (int)tx_id' in body


@pytest.mark.phase4
def test_phase4_extractor_unknown_method_raises():
    """A TB method call that doesn't match any known pattern raises."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _UnknownMethodTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "poly"

        def main(self) -> None:
            dut = PolyAccel()
            data_hdr = PolyCmdHdr()
            data_hdr.bogus_method("foo")
            dut.run()

    tb = _UnknownMethodTB(name='tb')
    with pytest.raises(SynthesisError):
        extract_testbench(tb)


@pytest.mark.phase4
def test_phase4_count_kwarg_required_for_array_ops():
    """Array-mode TB calls require count=...; omitting it is a hard error."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _MissingCountTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "poly"

        def main(self) -> None:
            dut = PolyAccel()
            samp_in = SampArray()
            samp_in.read_uint32_file_array(self.data_dir + "/x.bin")
            dut.run()

    tb = _MissingCountTB(name='tb')
    with pytest.raises(SynthesisError, match="requires count"):
        extract_testbench(tb)


# ---------------------------------------------------------------------------
# write_status_json silently drops is_vitis_auto fields
# ---------------------------------------------------------------------------
#
# Vitis HLS auto-generates ap_start/ap_done inside the s_axilite control
# register — they are not C++ kernel parameters and the generated TB
# cannot read them as locals. Listing them in fields=[...] should be a
# no-op (a user writing the symmetric shape on both flows is idiomatic;
# requiring them to manually exclude is a foot-gun).

def _make_regmap_auto_dut_class():
    """Build a tiny regmap-only DUT class with one user field, used by the
    is_vitis_auto-filter tests. Defined as a factory because dataclass
    needs a module-level home to resolve ClassVar annotations under
    ``from __future__ import annotations``; we put it on the module
    namespace below.
    """
    return _RegmapAutoDut


from dataclasses import dataclass as _dc
from typing import ClassVar as _CV

from waveflow.hw.dataschema import IntField as _IntField
from waveflow.hw.hw_module import HwModule as _HwComp
from waveflow.hw.regmap import (
    RegAccess as _RA,
    RegField as _RF,
    VitisRegMap as _VRM,
    VitisRegMapMMIFSlave as _VRMS,
)
from waveflow.simulation.simobj import ProcessGen as _PG

_S32_TB = _IntField.specialize(bitwidth=32, signed=True)


@_dc
class _RegmapAutoDut(_HwComp):
    cpp_kernel_name: _CV[str | None] = "rmauto"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = _VRM({
            "y": _RF(_S32_TB, _RA.R, description="user field"),
        })
        self.s_lite = _VRMS(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        self.add_endpoint(self.s_lite)

    def on_start(self) -> _PG[None]:
        self.regmap.set("y", 0)


@pytest.mark.phase4
def test_write_status_json_drops_is_vitis_auto_fields():
    """``fields=["ap_done", "ap_start", "y"]`` lowers to a TB that only
    references the user field ``y``. The auto-managed ap_* bits are not
    C++ locals on the Vitis side, so listing them in the symmetric
    Python/C++ shape is fine — the parse pass silently filters them.
    """
    from waveflow.build.hwgen import tb_files_to_str

    @dataclass
    class _AutoFilterTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "rmauto"

        def main(self) -> None:
            dut = _RegmapAutoDut()
            dut.run()
            dut.regmap.write_status_json(
                self.data_dir + "/regmap_status.json",
                fields=["ap_done", "ap_start", "y"],
            )

    files = tb_files_to_str(_AutoFilterTB, output_dir="gen")
    body = files["rmauto_tb.cpp"]

    # The user field IS emitted.
    assert r'\"y\": " << (int)y' in body
    # The auto-managed bits are NOT emitted as locals — they would not
    # compile against the Vitis-generated kernel signature.
    assert "ap_done" not in body
    assert "ap_start" not in body


@pytest.mark.phase4
def test_write_status_json_filter_emits_debug_log():
    """Surfacing the dropped fields via a debug log keeps the silent
    filter discoverable for anyone reading the trace.
    """
    import logging

    from waveflow.build.hwgen import tb_files_to_str

    @dataclass
    class _LogFilterTB(HwTestbench):
        cpp_kernel_name: ClassVar[str | None] = "rmauto"

        def main(self) -> None:
            dut = _RegmapAutoDut()
            dut.run()
            dut.regmap.write_status_json(
                self.data_dir + "/regmap_status.json",
                fields=["ap_done", "y"],
            )

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("waveflow.build.hwcodegen")
    handler = _Capture(level=logging.DEBUG)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        tb_files_to_str(_LogFilterTB, output_dir="gen")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    matches = [r for r in records if "ap_done" in r.getMessage()]
    assert matches, (
        "expected at least one debug log mentioning the dropped ap_done "
        f"field; got: {[r.getMessage() for r in records]}"
    )


# ---------------------------------------------------------------------------
# Stage 1 — the extractor accepts a yielding (process-form) main().
#
# A single-process ``SeqTB.main()`` may ``yield`` to model timing, using the
# process forms ``run_once_sim`` / ``write`` / ``get``.  Those forms lower to
# the SAME C++ the synchronous ``run_once`` / ``push`` / ``pop`` forms emit —
# the equivalence proof below diffs a yielding rewrite against its synchronous
# twin byte-for-byte.
# ---------------------------------------------------------------------------

from examples.regmap.simp_fun import SimpFun, SimpFunTBHls  # noqa: E402
from examples.stream_inband.poly import (  # noqa: E402
    PolyAccel as _PolyAccel,
    PolyCmdHdr as _PolyCmdHdr,
    PolyRespHdr as _PolyRespHdr,
    PolyTBHls as _PolyTBHls,
    SampArray as _SampArray,
)


@dataclass
class _SimpFunTBProc(SeqTB):
    """Yielding rewrite of :class:`SimpFunTBHls`: a stripped
    ``yield self.timeout(...)`` (op 1) and a captured
    ``y = yield from dut.run_once_sim(...)`` (op 2) in place of the synchronous
    ``dut.run_once(...)``."""

    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self):  # type: ignore[override]
        dut = SimpFun()
        dut.regmap.read_uint32_file("x", self.data_dir + "/x.bin")
        dut.regmap.read_uint32_file("a", self.data_dir + "/a.bin")
        dut.regmap.read_uint32_file("b", self.data_dir + "/b.bin")
        yield self.timeout(4)
        # `y` captures the kernel's output field — the point of op 2.  It is
        # deliberately unused here (this TB reads outputs via write_status_json);
        # the capture must lower to no extra C++ to stay byte-identical.
        y = yield from dut.run_once_sim(  # noqa: F841
            dut.regmap.get("x"), dut.regmap.get("a"), dut.regmap.get("b"))
        dut.regmap.write_status_json(
            self.data_dir + "/regmap_status.json",
            fields=["ap_done", "y"],
        )


@dataclass
class _PolyTBProc(SeqTB):
    """Yielding rewrite of :class:`PolyTBHls` exercising the stream process
    forms (op 3): ``yield from ep.write(...)`` for the input pushes and
    ``x = yield from ep.get(Schema[, count=...])`` for the output pops."""

    cpp_kernel_name: ClassVar[str | None] = "poly"

    def main(self):  # type: ignore[override]
        dut = _PolyAccel()
        dut.regmap.read_uint32_file_array(
            "coeffs", self.data_dir + "/coeffs.bin", count=4)
        data_hdr = _PolyCmdHdr()
        data_hdr.read_uint32_file(self.data_dir + "/data_cmd_hdr.bin")
        samp_in = _SampArray()
        samp_in.read_uint32_file_array(
            self.data_dir + "/samp_in_data.bin", count=data_hdr.nsamp)
        end_hdr = _PolyCmdHdr()
        end_hdr.read_uint32_file(self.data_dir + "/end_cmd_hdr.bin")

        yield from dut.s_in.write(data_hdr)
        yield from dut.s_in.write(samp_in, count=data_hdr.nsamp)
        yield from dut.s_in.write(end_hdr)

        dut.run()

        resp_hdr = yield from dut.m_out.get(_PolyRespHdr)
        samp_out = yield from dut.m_out.get(_SampArray, count=data_hdr.nsamp)

        resp_hdr.write_uint32_file(self.data_dir + "/resp_hdr_data.bin")
        samp_out.write_uint32_file_array(
            self.data_dir + "/samp_out_data.bin", count=data_hdr.nsamp)

        dut.regmap.write_status_json(
            self.data_dir + "/regmap_status.json",
            fields=["halted", "error", "tx_id"])


def test_run_once_sim_capture_equals_run_once_byte_for_byte():
    """Op 2: ``[y =] yield from dut.run_once_sim(...)`` (plus a stripped
    ``yield self.timeout(...)``) lowers to the exact same ``*_tb.cpp`` as the
    synchronous ``dut.run_once(...)`` twin."""
    from waveflow.build.hwgen import tb_files_to_str
    proc = tb_files_to_str(_SimpFunTBProc)
    sync = tb_files_to_str(SimpFunTBHls)
    assert proc == sync


def test_stream_write_get_equals_push_pop_byte_for_byte():
    """Op 3: ``yield from ep.write(...)`` / ``x = yield from ep.get(...)`` lower
    to the exact same ``*_tb.cpp`` as the synchronous ``ep.push`` / ``ep.pop``
    twin (scalar and array forms)."""
    from waveflow.build.hwgen import tb_files_to_str
    proc = tb_files_to_str(_PolyTBProc)
    sync = tb_files_to_str(_PolyTBHls)
    assert proc == sync


def test_timeout_yield_is_stripped():
    """Op 1: a bare ``yield self.timeout(...)`` (a @sim_only latency model)
    carries no hardware meaning and is stripped in testbench mode — the emitted
    C++ contains no trace of it."""
    from waveflow.build.hwgen import tb_files_to_str
    body = tb_files_to_str(_SimpFunTBProc)["simp_fun_tb.cpp"]
    assert "timeout" not in body


def test_run_once_sim_and_run_once_lower_identically():
    """The IR-level guarantee behind op 2: both invocation forms emit the same
    kernel call statement."""
    from waveflow.build.hwgen import tb_files_to_str
    assert (
        tb_files_to_str(_SimpFunTBProc)["simp_fun_tb.cpp"]
        == tb_files_to_str(SimpFunTBHls)["simp_fun_tb.cpp"]
    )
    assert "simp_fun(x, a, b, y);" in tb_files_to_str(_SimpFunTBProc)["simp_fun_tb.cpp"]


# ---------------------------------------------------------------------------
# Stage 2b — read an input file into a plain local (drop the round-trip).
#
# ``x = Int32().read_uint32_file(path)`` is the standard schema file-IO
# spelling, so it needs no new runtime — a run of ``main()`` gets a real
# ``Int32`` back and passes it straight into ``run_once_sim(x, a, b)``, instead
# of loading the DUT's regmap fields and reading them back out again.  In
# codegen the local aliases the input field's C++ local (the input-side mirror
# of the Stage-1 output capture), so it lowers to the C++ the round-trip form
# already emitted.
# ---------------------------------------------------------------------------

from examples.regmap.simp_fun import Int32 as _Int32  # noqa: E402
from waveflow.hw.dataschema import IntField as _IntField  # noqa: E402

#: A schema that is *not* any simp_fun field's type.  Module-level because the
#: extractor resolves a TB's class references out of ``main.__globals__``.
_Uint8 = _IntField.specialize(bitwidth=8, signed=False)


@dataclass
class _SimpFunTBRoundTrip(SeqTB):
    """The pre-Stage-2b ``SimpFunTBHls``: inputs go into the DUT's regmap fields
    and come back out via ``dut.regmap.get(...)`` — the round-trip Stage 2b removes."""

    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self):  # type: ignore[override]
        dut = SimpFun()
        dut.regmap.read_uint32_file("x", self.data_dir + "/x.bin")
        dut.regmap.read_uint32_file("a", self.data_dir + "/a.bin")
        dut.regmap.read_uint32_file("b", self.data_dir + "/b.bin")
        yield from dut.run_once_sim(
            dut.regmap.get("x"), dut.regmap.get("a"), dut.regmap.get("b"))
        dut.regmap.write_status_json(
            self.data_dir + "/regmap_status.json",
            fields=["ap_done", "y"],
        )


def test_file_read_into_local_equals_regmap_round_trip_byte_for_byte():
    """The payoff, and the gate: reading each input into a local and passing the
    values straight in generates the *same* ``simp_fun_tb.cpp`` as the round-trip
    twin — the round-trip was only ever a Python-side detour."""
    from waveflow.build.hwgen import tb_files_to_str
    assert tb_files_to_str(SimpFunTBHls) == tb_files_to_str(_SimpFunTBRoundTrip)


def test_file_read_into_local_fills_the_kernel_input_arg():
    """The local is the kernel's input arg: the read lands in the field local the
    call passes, so no extra declaration or copy is emitted."""
    from waveflow.build.hwgen import tb_files_to_str
    body = tb_files_to_str(SimpFunTBHls)["simp_fun_tb.cpp"]
    assert "simp_fun(x, a, b, y);" in body
    assert body.count("ap_int<32> x = 0;") == 1
    assert 'data_dir + std::string("/x.bin")' in body


def test_file_read_local_must_name_an_input_field():
    """A read into a local that names no input field of a bound DUT is rejected —
    v1 has no lowering for a free-standing local (it would need its own C++ decl
    and an arg-carrying kernel call)."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _BadLocalTB(SeqTB):
        cpp_kernel_name: ClassVar[str | None] = "simp_fun"

        def main(self):  # type: ignore[override]
            dut = SimpFun()
            z = _Int32().read_uint32_file(self.data_dir + "/z.bin")
            yield from dut.run_once_sim(z, z, z)

    with pytest.raises(SynthesisError, match="no bound DUT has an input field 'z'"):
        extract_testbench(_BadLocalTB(name='tb'))


def test_file_read_local_cannot_name_an_output_field():
    """Output (``R``) fields are the kernel's out-params, not inputs to fill."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _OutputFieldTB(SeqTB):
        cpp_kernel_name: ClassVar[str | None] = "simp_fun"

        def main(self):  # type: ignore[override]
            dut = SimpFun()
            y = _Int32().read_uint32_file(self.data_dir + "/y.bin")
            yield from dut.run_once_sim(y, y, y)

    with pytest.raises(SynthesisError, match="no bound DUT has an input field 'y'"):
        extract_testbench(_OutputFieldTB(name='tb'))


def test_file_read_schema_must_match_the_field():
    """The read must fill the field it is passed to, so the schema must be the
    field's declared type — a mismatch would silently emit a wrong-width read."""
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    @dataclass
    class _WrongSchemaTB(SeqTB):
        cpp_kernel_name: ClassVar[str | None] = "simp_fun"

        def main(self):  # type: ignore[override]
            dut = SimpFun()
            x = _Uint8().read_uint32_file(self.data_dir + "/x.bin")
            yield from dut.run_once_sim(x, x, x)

    with pytest.raises(SynthesisError, match="is declared as Int32"):
        extract_testbench(_WrongSchemaTB(name='tb'))
