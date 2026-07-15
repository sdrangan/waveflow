"""Tests for the C++ codegen pass (waveflow/build/hwgen.py)."""
from __future__ import annotations

from dataclasses import dataclass as _dataclass
from enum import IntEnum
from typing import ClassVar

import pytest

from waveflow.build.hwgen import CodegenCtx, to_cpp
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.hwstmt import (
    CaseStmt,
    ContinueStmt,
    HwVar,
    ReturnStmt,
    SeqStmt,
    WhileStmt,
)
from waveflow.hw.dataschema import DataList as _DataList
from waveflow.hw.dataschema import IntField as _IntField
from waveflow.hw.interface import StreamIFMaster as _StreamIFMaster
from waveflow.hw.interface import StreamIFSlave as _StreamIFSlave
from waveflow.hw.regmap import (
    Bit as _Bit,
    RegAccess as _RegAccess,
    RegField as _RegField,
    VitisRegMap as _VitisRegMap,
    VitisRegMapMMIFSlave as _VitisRegMapMMIFSlave,
)
from waveflow.hw.synth import synthesizable as _synthesizable
from waveflow.simulation.simobj import ProcessGen as _ProcessGen
from waveflow.simulation.simulation import Simulation
from tests.hw.test_resolve import DemoCmdHdr
from tests.hw.test_resolve import DemoCmdHdr as _DemoCmdHdr
from tests.hw.test_resolve import DemoError as _DemoError
from tests.hw.test_resolve import DemoErrorField


class DemoCmdType(IntEnum):
    DATA = 0
    END  = 1


class DemoError(IntEnum):
    OK         = 0
    BAD_INPUT  = 1


def _ctx() -> CodegenCtx:
    comp = HwComponent(name='c', sim=Simulation())
    return CodegenCtx(comp=comp)


# ---------------------------------------------------------------------------
# Phase 1: control-flow statements
# ---------------------------------------------------------------------------

def test_return_no_value():
    assert to_cpp(ReturnStmt(value=None), _ctx()) == "    return;"


def test_continue_stmt():
    assert to_cpp(ContinueStmt(), _ctx()) == "    continue;"


def test_while_with_return_body():
    stmt = WhileStmt(body=SeqStmt(stmts=[ReturnStmt(value=None)]))
    expected = (
        "    while (true) {\n"
        "        return;\n"
        "    }"
    )
    assert to_cpp(stmt, _ctx()) == expected


def test_case_stmt_bare_var():
    stmt = CaseStmt(
        var=HwVar(name='err', typ=None),
        field=None,
        value=DemoError.OK,
        op='!=',
        if_true=SeqStmt(stmts=[ReturnStmt(value=None)]),
    )
    expected = (
        "    if (err != DemoError::OK) {\n"
        "        return;\n"
        "    }"
    )
    assert to_cpp(stmt, _ctx()) == expected


def test_case_stmt_field_access():
    stmt = CaseStmt(
        var=HwVar(name='cmd', typ=None),
        field='cmd_type',
        value=DemoCmdType.END,
        op='==',
        if_true=SeqStmt(stmts=[ReturnStmt(value=None)]),
    )
    expected = (
        "    if (cmd.cmd_type == DemoCmdType::END) {\n"
        "        return;\n"
        "    }"
    )
    assert to_cpp(stmt, _ctx()) == expected


def test_seq_stmt_joins_with_newlines():
    stmt = SeqStmt(stmts=[ReturnStmt(value=None), ContinueStmt()])
    assert to_cpp(stmt, _ctx()) == "    return;\n    continue;"


def test_case_stmt_with_else():
    stmt = CaseStmt(
        var=HwVar(name='err', typ=None),
        field=None,
        value=DemoError.OK,
        op='==',
        if_true=SeqStmt(stmts=[ContinueStmt()]),
        if_false=SeqStmt(stmts=[ReturnStmt(value=None)]),
    )
    expected = (
        "    if (err == DemoError::OK) {\n"
        "        continue;\n"
        "    } else {\n"
        "        return;\n"
        "    }"
    )
    assert to_cpp(stmt, _ctx()) == expected


def test_unhandled_stmt_raises_not_implemented():
    class _Bogus:
        pass

    with pytest.raises(NotImplementedError):
        to_cpp(_Bogus(), _ctx())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Phase 2: Stream statements
# ---------------------------------------------------------------------------

class _FakeSchema:
    @classmethod
    def cpp_class_name(cls) -> str:
        return "DemoCmdHdr"


class _FakeEndpoint:
    """Stand-in object used as the bound `self` of a stream method."""
    bitwidth: int = 32


class _FakeBoundMethod:
    """Carries ``__self__`` so ``_endpoint_name`` can locate the endpoint."""
    def __init__(self, endpoint):
        self.__self__ = endpoint


def _comp_with_endpoints(**endpoints):
    """Create a HwComponent and stash endpoints as attributes for vars() lookup."""
    comp = HwComponent(name='c', sim=Simulation())
    for name, ep in endpoints.items():
        setattr(comp, name, ep)
    return comp


def test_stream_get_emits_decl_and_read():
    from waveflow.hw.interface import StreamGetStmt
    s_in = _FakeEndpoint()
    comp = _comp_with_endpoints(s_in=s_in)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamGetStmt(
        method=_FakeBoundMethod(s_in),
        inputs=[_FakeSchema],
        outputs=[HwVar(name='cmd', typ=_FakeSchema)],
    )
    expected = (
        "    DemoCmdHdr cmd;\n"
        "    cmd.read_axi4_stream<32>(s_in);"
    )
    assert to_cpp(stmt, ctx) == expected


def test_stream_write_emits_call():
    from waveflow.hw.interface import StreamWriteStmt
    m_out = _FakeEndpoint()
    comp = _comp_with_endpoints(m_out=m_out)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamWriteStmt(
        method=_FakeBoundMethod(m_out),
        inputs=[HwVar(name='resp', typ=None)],
        outputs=[],
    )
    assert to_cpp(stmt, ctx) == "    resp.write_axi4_stream<32>(m_out, true);"


def test_stream_drain_emits_flush():
    from waveflow.hw.interface import StreamDrainStmt
    s_in = _FakeEndpoint()
    comp = _comp_with_endpoints(s_in=s_in)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamDrainStmt(
        method=_FakeBoundMethod(s_in),
        inputs=[],
        outputs=[],
    )
    assert to_cpp(stmt, ctx) == (
        "    streamutils::flush_axi4_stream_to_tlast<32>(s_in);"
    )


class _FakeParamEndpoint:
    """Stand-in endpoint whose bitwidth is a HwParamValue."""
    def __init__(self, param_name: str, value: int = 32) -> None:
        from waveflow.hw.hw_component import HwParamValue
        self.bitwidth = HwParamValue(value, param_name)


def test_stream_get_uses_literal_bitwidth_from_hwparam_endpoint():
    """Top-level kernels are now concrete: stream stmts emit the literal
    bitwidth of the endpoint's ``HwParamValue``, not the param name."""
    from waveflow.hw.interface import StreamGetStmt
    s_in = _FakeParamEndpoint(param_name="in_bw", value=64)
    comp = _comp_with_endpoints(s_in=s_in)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamGetStmt(
        method=_FakeBoundMethod(s_in),
        inputs=[_FakeSchema],
        outputs=[HwVar(name='cmd', typ=_FakeSchema)],
    )
    out = to_cpp(stmt, ctx)
    assert "read_axi4_stream<64>(s_in)" in out
    assert "<in_bw>" not in out


def test_stream_write_uses_literal_bitwidth_from_hwparam_endpoint():
    from waveflow.hw.interface import StreamWriteStmt
    m_out = _FakeParamEndpoint(param_name="out_bw", value=64)
    comp = _comp_with_endpoints(m_out=m_out)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamWriteStmt(
        method=_FakeBoundMethod(m_out),
        inputs=[HwVar(name='resp', typ=None)],
        outputs=[],
    )
    out = to_cpp(stmt, ctx)
    assert "write_axi4_stream<64>(m_out, true)" in out
    assert "<out_bw>" not in out


def test_stream_drain_uses_literal_bitwidth_from_hwparam_endpoint():
    from waveflow.hw.interface import StreamDrainStmt
    s_in = _FakeParamEndpoint(param_name="in_bw", value=64)
    comp = _comp_with_endpoints(s_in=s_in)
    ctx = CodegenCtx(comp=comp)
    stmt = StreamDrainStmt(
        method=_FakeBoundMethod(s_in),
        inputs=[],
        outputs=[],
    )
    out = to_cpp(stmt, ctx)
    assert "flush_axi4_stream_to_tlast<64>(s_in)" in out
    assert "<in_bw>" not in out


# ---------------------------------------------------------------------------
# Phase 3: Regmap statements
# ---------------------------------------------------------------------------

def test_regmap_set_literal():
    from waveflow.hw.regmap import RegMapSetStmt
    stmt = RegMapSetStmt(
        method=None,
        inputs=['halted', 1],
        outputs=[],
    )
    assert to_cpp(stmt, _ctx()) == "    halted = 1;"


def test_regmap_set_hwvar():
    from waveflow.hw.regmap import RegMapSetStmt
    stmt = RegMapSetStmt(
        method=None,
        inputs=['error', HwVar(name='err', typ=DemoError)],
        outputs=[],
    )
    assert to_cpp(stmt, _ctx()) == "    error = err;"


def test_regmap_set_field_ref():
    from waveflow.hw.hwstmt import FieldRef
    from waveflow.hw.regmap import RegMapSetStmt
    stmt = RegMapSetStmt(
        method=None,
        inputs=['tx_id', FieldRef(var=HwVar(name='cmd', typ=None), field='tx_id')],
        outputs=[],
    )
    assert to_cpp(stmt, _ctx()) == "    tx_id = cmd.tx_id;"


def test_regmap_get_with_typed_output():
    from waveflow.hw.regmap import RegMapGetStmt

    class _CoeffArray:
        @classmethod
        def cpp_class_name(cls):
            return "CoeffArray"

    # Renamed binding (out.name != field_name): a copy/binding is emitted.
    stmt = RegMapGetStmt(
        method=None,
        inputs=['coeffs'],
        outputs=[HwVar(name='local_coeffs', typ=_CoeffArray)],
    )
    assert to_cpp(stmt, _ctx()) == "    CoeffArray local_coeffs = coeffs;"


def test_regmap_get_with_untyped_output_falls_back_to_auto():
    from waveflow.hw.regmap import RegMapGetStmt
    stmt = RegMapGetStmt(
        method=None,
        inputs=['coeffs'],
        outputs=[HwVar(name='local_coeffs', typ=None)],
    )
    assert to_cpp(stmt, _ctx()) == "    auto local_coeffs = coeffs;"


def test_regmap_get_same_name_as_field_emits_noop_comment():
    """When the bound HwVar name equals the field name, the kernel signature
    parameter is already in scope — no redundant copy/self-init."""
    from waveflow.hw.regmap import RegMapGetStmt
    stmt = RegMapGetStmt(
        method=None,
        inputs=['coeffs'],
        outputs=[HwVar(name='coeffs', typ=None)],
    )
    out = to_cpp(stmt, _ctx())
    assert "coeffs is already in scope" in out
    assert "auto coeffs = coeffs" not in out
    assert "= coeffs;" not in out


# ---------------------------------------------------------------------------
# Phase 4: FunctionStmt call sites
# ---------------------------------------------------------------------------

class _FakeMethod:
    def __init__(self, name):
        self.__name__ = name


def test_function_stmt_with_typed_output():
    from waveflow.hw.hwstmt import FunctionStmt
    stmt = FunctionStmt(
        method=_FakeMethod('process'),
        inputs=[HwVar(name='cmd', typ=None)],
        outputs=[HwVar(name='err', typ=DemoError)],
    )
    # _ctx() uses bare HwComponent → cpp_kernel_name "hw" -> namespace "hw_impl".
    # IntEnum return types render as ap_uint<8> to match what the hook
    # forward decl actually returns (cpp_type maps IntEnum → ap_uint<8>).
    assert to_cpp(stmt, _ctx()) == "    ap_uint<8> err = hw_impl::process(cmd);"


def test_function_stmt_no_outputs():
    from waveflow.hw.hwstmt import FunctionStmt
    stmt = FunctionStmt(
        method=_FakeMethod('process'),
        inputs=[HwVar(name='cmd', typ=None)],
        outputs=[],
    )
    assert to_cpp(stmt, _ctx()) == "    hw_impl::process(cmd);"


def test_function_stmt_with_endpoint_arg():
    from waveflow.hw.hwstmt import FunctionStmt
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    sim = Simulation()
    s_in = StreamIFSlave(name='s_in_ep', sim=sim, bitwidth=32)
    m_out = StreamIFMaster(name='m_out_ep', sim=sim, bitwidth=32)
    comp = HwComponent(name='c', sim=sim)
    comp.s_in = s_in
    comp.m_out = m_out
    ctx = CodegenCtx(comp=comp)
    stmt = FunctionStmt(
        method=_FakeMethod('process'),
        inputs=[HwVar(name='cmd', typ=None), s_in, m_out],
        outputs=[],
    )
    assert to_cpp(stmt, ctx) == "    hw_impl::process(cmd, s_in, m_out);"


def test_function_stmt_opt_out_no_qualifier():
    from waveflow.hw.hwstmt import FunctionStmt

    class _NoNs(HwComponent):
        cpp_namespace: ClassVar[str | None] = ""

    sim = Simulation()
    comp = _NoNs(name='c', sim=sim)
    ctx = CodegenCtx(comp=comp)
    stmt = FunctionStmt(
        method=_FakeMethod('process'),
        inputs=[HwVar(name='cmd', typ=None)],
        outputs=[],
    )
    assert to_cpp(stmt, ctx) == "    process(cmd);"


def test_function_stmt_custom_namespace():
    from waveflow.hw.hwstmt import FunctionStmt

    class _CustomNs(HwComponent):
        cpp_namespace: ClassVar[str | None] = "custom"

    sim = Simulation()
    comp = _CustomNs(name='c', sim=sim)
    ctx = CodegenCtx(comp=comp)
    stmt = FunctionStmt(
        method=_FakeMethod('process'),
        inputs=[HwVar(name='cmd', typ=None)],
        outputs=[],
    )
    assert to_cpp(stmt, ctx) == "    custom::process(cmd);"


def test_function_stmt_multi_output_raises():
    from waveflow.hw.hwstmt import FunctionStmt
    stmt = FunctionStmt(
        method=_FakeMethod('split'),
        inputs=[HwVar(name='x', typ=None)],
        outputs=[
            HwVar(name='a', typ=None),
            HwVar(name='b', typ=None),
        ],
    )
    with pytest.raises(NotImplementedError, match="Multi-output"):
        to_cpp(stmt, _ctx())


# ---------------------------------------------------------------------------
# Phase 5: kernel_body_to_cpp end-to-end
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Kernel-files Phase 1: cpp_type + cpp_kernel_name
# ---------------------------------------------------------------------------

def test_cpp_type_intfield_unsigned():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import IntField
    assert cpp_type(IntField.specialize(bitwidth=16, signed=False)) == "ap_uint<16>"


def test_cpp_type_intfield_signed():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import IntField
    assert cpp_type(IntField.specialize(bitwidth=8, signed=True)) == "ap_int<8>"


def test_cpp_type_floatfield_32():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import FloatField
    assert cpp_type(FloatField.specialize(bitwidth=32)) == "float"


def test_cpp_type_floatfield_64():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import FloatField
    assert cpp_type(FloatField.specialize(bitwidth=64)) == "double"


def test_cpp_type_enumfield():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import EnumField
    assert cpp_type(EnumField.specialize(enum_type=DemoError)) == "ap_uint<8>"


def test_cpp_type_intenum_direct():
    """IntEnum subclasses (e.g. resolved FunctionStmt return types) map to ap_uint<8>."""
    from waveflow.build.hwgen import cpp_type
    assert cpp_type(DemoError) == "ap_uint<8>"


def test_cpp_type_dataschema_subclass_uses_cpp_class_name():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import DataList, IntField

    class MyMsg(DataList):
        elements = {
            "x": {"schema": IntField.specialize(bitwidth=8, signed=False)},
        }

    assert cpp_type(MyMsg) == "MyMsg"


def test_cpp_type_none_raises():
    from waveflow.build.hwgen import cpp_type
    with pytest.raises(RuntimeError):
        cpp_type(None)


def test_snake_case_helper():
    from waveflow.build.hwgen import _snake_case
    assert _snake_case("PolyCmdHdr") == "poly_cmd_hdr"
    assert _snake_case("Float32") == "float32"
    assert _snake_case("CoeffArray") == "coeff_array"
    assert _snake_case("DemoErrorField") == "demo_error_field"
    # Already-snake names pass through.
    assert _snake_case("already_snake") == "already_snake"


def test_cpp_kernel_name_demo():
    from waveflow.build.hwgen import cpp_kernel_name
    from tests.hw.test_resolve import DemoComponent
    assert cpp_kernel_name(DemoComponent) == "demo"


def test_cpp_kernel_name_poly_accel_uses_override():
    """``PolyAccelComponent`` overrides ``cpp_kernel_name`` to ``"poly"``."""
    from waveflow.build.hwgen import cpp_kernel_name
    import sys
    from pathlib import Path
    POLY_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"
    if str(POLY_DIR) not in sys.path:
        sys.path.insert(0, str(POLY_DIR))
    from poly import PolyAccelComponent
    assert cpp_kernel_name(PolyAccelComponent) == "poly"


def test_cpp_kernel_name_override():
    from waveflow.build.hwgen import cpp_kernel_name

    class _Overridden(HwComponent):
        cpp_kernel_name: ClassVar[str | None] = "my_custom_name"

    assert cpp_kernel_name(_Overridden) == "my_custom_name"


# ---------------------------------------------------------------------------
# Namespace Phase 1: resolved_namespace
# ---------------------------------------------------------------------------

def test_resolved_namespace_default_is_kernel_name_plus_impl():
    """The default must DERIVE from the kernel name without EQUALLING it.

    A namespace and a function cannot share a name in the same scope — emitting
    `void demo(...)` beside `namespace demo {...}` is ill-formed C++ ("redeclared as
    different kind of entity").  This test previously asserted the namespace WAS the
    kernel name, which pinned exactly that bug; it went unnoticed because every
    hook-emitting component overrides `cpp_namespace` by hand, so the default had never
    been used with a hook.
    """
    from waveflow.build.hwgen import cpp_kernel_name, resolved_namespace
    from tests.hw.test_resolve import DemoComponent
    assert resolved_namespace(DemoComponent) == "demo_impl"
    assert resolved_namespace(DemoComponent) != cpp_kernel_name(DemoComponent)


def test_resolved_namespace_explicit_string():
    from waveflow.build.hwgen import resolved_namespace

    class _NsCustom(HwComponent):
        cpp_namespace: ClassVar[str | None] = "custom"

    assert resolved_namespace(_NsCustom) == "custom"


def test_resolved_namespace_empty_opts_out():
    from waveflow.build.hwgen import resolved_namespace

    class _NsOptOut(HwComponent):
        cpp_namespace: ClassVar[str | None] = ""

    assert resolved_namespace(_NsOptOut) is None


def test_resolved_namespace_explicit_none_is_auto():
    from waveflow.build.hwgen import resolved_namespace

    class _NsAuto(HwComponent):
        cpp_namespace: ClassVar[str | None] = None

    # Auto-derives from cpp_kernel_name (default of HwComponent name is "hw"), with
    # `_impl` appended so the namespace cannot collide with the kernel function itself.
    assert resolved_namespace(_NsAuto) == f"{cpp_kernel_name_for(_NsAuto)}_impl"


def cpp_kernel_name_for(cls):
    from waveflow.build.hwgen import cpp_kernel_name
    return cpp_kernel_name(cls)


def test_resolved_namespace_independent_from_kernel_name_override():
    from waveflow.build.hwgen import cpp_kernel_name, resolved_namespace

    class _Both(HwComponent):
        cpp_namespace: ClassVar[str | None] = "alpha"
        cpp_kernel_name: ClassVar[str | None] = "beta"

    assert resolved_namespace(_Both) == "alpha"
    assert cpp_kernel_name(_Both) == "beta"


# ---------------------------------------------------------------------------
# Kernel-files Phase 2: hook_signature derivation
# ---------------------------------------------------------------------------
# Hook fixtures defined at module level so typing.get_type_hints() can
# resolve their annotations via the module's __globals__ (annotations are
# stringified under `from __future__ import annotations`).

@_synthesizable
def _hook_evaluate(self, cmd: _DemoCmdHdr) -> _ProcessGen[_DemoError]:
    yield None
    return _DemoError.OK


@_synthesizable
def _hook_with_stream(
    self,
    cmd: _DemoCmdHdr,
    s_in: _StreamIFSlave,
) -> _ProcessGen[None]:
    yield None


@_dataclass
class _TmplComp(HwComponent):
    """Component whose synthesizable hook takes a stream arg (triggers templating)."""

    cpp_kernel_name: ClassVar[str | None] = "tcomp"
    in_bw: HwParam[int] = 32

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = _StreamIFSlave(
            name=f'{self.name}_s_in', sim=self.sim, bitwidth=self.in_bw,
        )
        self.m_out = _StreamIFMaster(
            name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.in_bw,
        )
        self.regmap = _VitisRegMap({
            "halted": _RegField(_Bit, _RegAccess.R),
            "error": _RegField(DemoErrorField, _RegAccess.R),
        })
        self.s_lite = _VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

    def on_start(self) -> _ProcessGen[None]:
        while True:
            cmd: DemoCmdHdr = yield from self.s_in.get(DemoCmdHdr)
            if cmd.cmd_type == 0:
                return
            yield from self.process(cmd, self.s_in)
            return

    @_synthesizable
    def process(
        self,
        cmd: DemoCmdHdr,
        s_in: _StreamIFSlave,
    ) -> _ProcessGen[DemoError]:
        yield self.env.timeout(0)
        return DemoError.OK


@_synthesizable
def _hook_two_streams(
    self,
    cmd: _DemoCmdHdr,
    s_in: _StreamIFSlave,
    m_out: _StreamIFMaster,
) -> _ProcessGen[None]:
    yield None


@_synthesizable
def _hook_bad(self, x):  # no annotation
    return x


@_synthesizable
def _hook_no_return(self, cmd: _DemoCmdHdr):
    pass


def test_hook_signature_str_simple():
    from waveflow.build.hwgen import hook_signature_str
    assert hook_signature_str(_hook_evaluate) == (
        "ap_uint<8> _hook_evaluate(DemoCmdHdr cmd)"
    )


def test_hook_signature_str_with_stream_endpoint():
    from waveflow.build.hwgen import hook_signature_str
    sig = hook_signature_str(_hook_with_stream)
    assert "DemoCmdHdr cmd" in sig
    assert "hls::stream<streamutils::axi4s_word<WORD_BW>>& s_in" in sig
    assert sig.startswith("void _hook_with_stream(")


def test_hook_signature_missing_annotation_raises():
    from waveflow.build.hwgen import hook_signature
    with pytest.raises(RuntimeError, match="'x'"):
        hook_signature(_hook_bad)


def test_hook_signature_no_return_annotation_is_void():
    from waveflow.build.hwgen import hook_signature
    ret, _args = hook_signature(_hook_no_return)
    assert ret == "void"


# ---------------------------------------------------------------------------
# Kernel-files Phase 3: kernel_signature
# ---------------------------------------------------------------------------

def test_kernel_signature_demo_component():
    """Concrete top-level kernel: no ``template <...>`` block, literal
    bitwidths in the stream type expressions."""
    from waveflow.build.hwgen import kernel_signature
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    sig = kernel_signature(comp)
    expected_substrings = [
        "void demo(",
        "hls::stream<streamutils::axi4s_word<32>>& s_in",
        "hls::stream<streamutils::axi4s_word<32>>& m_out",
        "ap_uint<1>& halted",
        "ap_uint<8>& error",
        "ap_uint<16>& tx_id",
        "#pragma HLS INTERFACE axis port=s_in",
        "#pragma HLS INTERFACE axis port=m_out",
        "#pragma HLS INTERFACE s_axilite port=halted",
        "#pragma HLS INTERFACE s_axilite port=return",
    ]
    for sub in expected_substrings:
        assert sub in sig, f"Missing substring: {sub!r}\n--- sig ---\n{sig}"
    # No template at the top level (variant-based concrete emission).
    assert "template" not in sig
    # ap_start is_vitis_auto -> generated by `port=return bundle=control`, NOT
    # by an explicit argument. Verify it does not appear in the signature.
    assert "ap_start" not in sig, f"ap_start should not appear in signature:\n{sig}"
    # Concrete kernel uses literal bitwidths; the templated-name form must
    # NOT be present.
    assert "axi4s_word<in_bw>" not in sig
    assert "axi4s_word<out_bw>" not in sig


def test_kernel_signature_raises_on_name_collision():
    """A HwParam that shares a name with a regmap field triggers SynthesisError."""
    from dataclasses import dataclass

    from waveflow.build.hwcodegen import SynthesisError
    from waveflow.build.hwgen import kernel_signature
    from waveflow.hw.interface import StreamIFSlave
    from waveflow.hw.regmap import (
        Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave,
    )

    @dataclass
    class _CollidingComp(HwComponent):
        # 'halted' clashes with the regmap field name below.
        halted: HwParam[int] = 32

        def __post_init__(self) -> None:
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
            )
            self.regmap = VitisRegMap({
                "halted": RegField(Bit, RegAccess.R),
            })
            self.s_lite = VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=None,
            )
            for ep in (self.s_in, self.s_lite):
                self.add_endpoint(ep)

    comp = _CollidingComp(name="bad", sim=Simulation())
    with pytest.raises(SynthesisError, match="halted"):
        kernel_signature(comp)


# ---------------------------------------------------------------------------
# Kernel-files Phase 4: header_to_cpp
# ---------------------------------------------------------------------------

def test_header_to_cpp_demo_component_substrings():
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    hpp = header_to_cpp(type(comp))

    for sub in [
        "#pragma once",
        '#include "include/streamutils_hls.h"',
        '#include "include/demo_cmd_hdr.h"',
        "void demo(",
        ");",  # forward decl terminator
        "namespace demo_impl {",
        "ap_uint<8> process(DemoCmdHdr cmd);",
    ]:
        assert sub in hpp, f"Missing substring: {sub!r}\n--- hpp ---\n{hpp}"

    # Kernel decl must be outside (before) the namespace block;
    # the hook decl must be inside.
    kernel_idx = hpp.index("void demo(")
    ns_idx = hpp.index("namespace demo_impl {")
    process_idx = hpp.index("ap_uint<8> process(DemoCmdHdr cmd);")
    assert kernel_idx < ns_idx < process_idx


def test_header_to_cpp_forward_decl_has_no_pragmas():
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    # The .hpp must NOT include any HLS pragmas — those live in the .cpp.
    assert "#pragma HLS" not in hpp


# ---------------------------------------------------------------------------
# Kernel-files Phase 5: kernel_to_cpp + impl_stub_to_cpp + driver
# ---------------------------------------------------------------------------

def test_kernel_to_cpp_substrings():
    from waveflow.build.hwgen import kernel_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    cpp = kernel_to_cpp(type(comp))

    for sub in [
        '#include "demo.hpp"',
        "void demo(",
        "#pragma HLS INTERFACE axis port=s_in",
        "while (true)",
    ]:
        assert sub in cpp, f"Missing substring: {sub!r}\n--- cpp ---\n{cpp}"
    # Concrete top-level → no template anywhere in the .cpp.
    assert "template" not in cpp
    # No explicit instantiation tail any more — the function closes with `}`.
    assert cpp.rstrip().endswith("}")


def test_kernel_to_cpp_emits_one_kernel_per_variant():
    """``param_supports`` triggers additional concrete kernels."""
    from dataclasses import dataclass
    from typing import Any, ClassVar

    from waveflow.build.hwgen import kernel_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _VariantDemo(DemoComponent):
        cpp_kernel_name: ClassVar[str | None] = "vdemo"
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "bw64": {"in_bw": 64, "out_bw": 64},
        }

    cpp = kernel_to_cpp(_VariantDemo)
    # Default kernel: literal 32 (HwParam defaults).
    assert "void vdemo(" in cpp
    assert "axi4s_word<32>" in cpp
    # Variant kernel: bw64 suffix + literal 64 in stream types.
    assert "void vdemo_bw64(" in cpp
    assert "axi4s_word<64>" in cpp
    # No template at any level.
    assert "template" not in cpp


def test_header_to_cpp_emits_one_forward_decl_per_variant():
    from dataclasses import dataclass
    from typing import Any, ClassVar

    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _VariantDemoH(DemoComponent):
        cpp_kernel_name: ClassVar[str | None] = "vh"
        param_supports: ClassVar[dict[str, dict[str, Any]] | None] = {
            "bw64": {"in_bw": 64, "out_bw": 64},
        }

    hpp = header_to_cpp(_VariantDemoH)
    assert "void vh(" in hpp
    assert "void vh_bw64(" in hpp
    assert "axi4s_word<32>" in hpp
    assert "axi4s_word<64>" in hpp


def test_impl_stub_to_cpp_substrings():
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import _collect_hooks, impl_stub_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    tree = extract_kernel(comp)
    hooks = _collect_hooks(tree)
    assert len(hooks) == 1
    process = hooks[0]

    stub = impl_stub_to_cpp(comp, process)
    for sub in [
        '#include "demo.hpp"',
        "namespace demo_impl {",
        "ap_uint<8> process(DemoCmdHdr cmd) {",
        "// TODO: implement process",
        "return ap_uint<8>(0);",
    ]:
        assert sub in stub, f"Missing substring: {sub!r}\n--- stub ---\n{stub}"
    assert stub.rstrip().endswith("}")


def test_header_to_cpp_opt_out_emits_no_namespace_block():
    from typing import ClassVar
    from dataclasses import dataclass
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _OptOutDemo(DemoComponent):
        cpp_namespace: ClassVar[str | None] = ""

    comp = _OptOutDemo(name="optout", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert "namespace " not in hpp
    # The hook decl still appears, just not wrapped.
    assert "ap_uint<8> process(DemoCmdHdr cmd);" in hpp


def test_impl_stub_to_cpp_opt_out_emits_no_namespace_block():
    from typing import ClassVar
    from dataclasses import dataclass
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import _collect_hooks, impl_stub_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _OptOutDemo(DemoComponent):
        cpp_namespace: ClassVar[str | None] = ""

    comp = _OptOutDemo(name="optout", sim=Simulation())
    tree = extract_kernel(comp)
    process = _collect_hooks(tree)[0]
    stub = impl_stub_to_cpp(comp, process)
    assert "namespace " not in stub
    assert "ap_uint<8> process(DemoCmdHdr cmd) {" in stub


def test_header_to_cpp_custom_namespace():
    from typing import ClassVar
    from dataclasses import dataclass
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _CustomNsDemo(DemoComponent):
        cpp_namespace: ClassVar[str | None] = "custom"

    comp = _CustomNsDemo(name="cn", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert "namespace custom {" in hpp


def test_impl_stub_to_cpp_custom_namespace():
    from typing import ClassVar
    from dataclasses import dataclass
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import _collect_hooks, impl_stub_to_cpp
    from tests.hw.test_resolve import DemoComponent

    @dataclass
    class _CustomNsDemo(DemoComponent):
        cpp_namespace: ClassVar[str | None] = "custom"

    comp = _CustomNsDemo(name="cn", sim=Simulation())
    tree = extract_kernel(comp)
    process = _collect_hooks(tree)[0]
    stub = impl_stub_to_cpp(comp, process)
    assert "namespace custom {" in stub


def test_kernel_files_to_str_keys():
    from waveflow.build.hwgen import kernel_files_to_str
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    files = kernel_files_to_str(type(comp))
    assert set(files.keys()) == {
        "demo.hpp",
        "demo.cpp",
        "demo_process_impl.cpp",
    }


def test_kernel_body_to_cpp_demo_component_contains_expected_substrings():
    from waveflow.build.hwgen import kernel_body_to_cpp
    # Reuse the DemoComponent fixture from tests/hw/test_resolve.py.
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    body = kernel_body_to_cpp(comp)

    expected = [
        "while (true)",
        "DemoCmdHdr cmd;",
        # Concrete top-level kernel: literal bitwidth (32) substituted in.
        "cmd.read_axi4_stream<32>(s_in);",
        "if (cmd.cmd_type == DemoCmdType::END)",
        "return;",
        "ap_uint<8> err = demo_impl::process(cmd",
        # err is ap_uint<8>; cast the enum literal so the != comparison compiles.
        "if (err != (ap_uint<8>)static_cast<unsigned int>(DemoError::OK))",
        "error = err;",
        "tx_id = cmd.tx_id;",
        "halted = 1;",
    ]
    for sub in expected:
        assert sub in body, f"Missing substring: {sub!r}\n--- body ---\n{body}"
    # Literal '32' should no longer appear in the stream template arg.
    # Concrete top-level kernel uses literal 32 — assert the template-name
    # form is NOT present.
    assert "read_axi4_stream<in_bw>" not in body


def test_endpoint_name_not_found_raises():
    from waveflow.hw.interface import StreamGetStmt
    rogue = _FakeEndpoint()
    comp = _comp_with_endpoints()  # no endpoint set
    ctx = CodegenCtx(comp=comp)
    stmt = StreamGetStmt(
        method=_FakeBoundMethod(rogue),
        inputs=[_FakeSchema],
        outputs=[HwVar(name='x', typ=_FakeSchema)],
    )
    with pytest.raises(RuntimeError, match="not found"):
        to_cpp(stmt, ctx)


# ---------------------------------------------------------------------------
# Hook-template Phase 1: hook_template_params + single-call-site validation
# ---------------------------------------------------------------------------

def _stream_endpoint(name: str, bitwidth, slave: bool = True):
    """Build a real StreamIF endpoint with the given (possibly-HwParamValue) bitwidth."""
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    cls = StreamIFSlave if slave else StreamIFMaster
    return cls(name=name, sim=Simulation(), bitwidth=bitwidth)


def test_hook_template_params_empty_inputs():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hwstmt import FunctionStmt
    stmt = FunctionStmt(method=_FakeMethod("h"), inputs=[], outputs=[])
    assert hook_template_params(stmt) == []


def test_hook_template_params_hwvar_only_inputs():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hwstmt import FunctionStmt
    stmt = FunctionStmt(
        method=_FakeMethod("h"),
        inputs=[HwVar(name="x", typ=None), HwVar(name="y", typ=None)],
        outputs=[],
    )
    assert hook_template_params(stmt) == []


def test_hook_template_params_single_param_endpoint():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt
    ep = _stream_endpoint("s_in_ep", HwParamValue(32, "in_bw"))
    stmt = FunctionStmt(
        method=_FakeMethod("h"),
        inputs=[HwVar(name="cmd", typ=None), ep],
        outputs=[],
    )
    assert hook_template_params(stmt) == ["in_bw"]


def test_hook_template_params_two_endpoints_same_param_dedupes():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt
    bw = HwParamValue(32, "in_bw")
    e1 = _stream_endpoint("a", bw)
    e2 = _stream_endpoint("b", bw, slave=False)
    stmt = FunctionStmt(method=_FakeMethod("h"), inputs=[e1, e2], outputs=[])
    assert hook_template_params(stmt) == ["in_bw"]


def test_hook_template_params_two_endpoints_different_params():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt
    e1 = _stream_endpoint("a", HwParamValue(32, "in_bw"))
    e2 = _stream_endpoint("b", HwParamValue(64, "out_bw"), slave=False)
    stmt = FunctionStmt(method=_FakeMethod("h"), inputs=[e1, e2], outputs=[])
    assert hook_template_params(stmt) == ["in_bw", "out_bw"]


def test_hook_template_params_raw_int_endpoint_skipped():
    from waveflow.build.hwgen import hook_template_params
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt
    raw = _stream_endpoint("raw", 32)
    param = _stream_endpoint("p", HwParamValue(32, "in_bw"))
    stmt = FunctionStmt(method=_FakeMethod("h"), inputs=[raw, param], outputs=[])
    assert hook_template_params(stmt) == ["in_bw"]


def test_validate_single_call_site_single_site_ok():
    from waveflow.build.hwgen import _validate_single_call_site
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt, SeqStmt
    method = _FakeMethod("h")
    ep = _stream_endpoint("a", HwParamValue(32, "in_bw"))
    tree = SeqStmt(stmts=[
        FunctionStmt(method=method, inputs=[ep], outputs=[]),
    ])
    _validate_single_call_site(tree, method, ["in_bw"])


def test_validate_single_call_site_consistent_ok():
    from waveflow.build.hwgen import _validate_single_call_site
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt, SeqStmt
    method = _FakeMethod("h")
    ep = _stream_endpoint("a", HwParamValue(32, "in_bw"))
    tree = SeqStmt(stmts=[
        FunctionStmt(method=method, inputs=[ep], outputs=[]),
        FunctionStmt(method=method, inputs=[ep], outputs=[]),
    ])
    _validate_single_call_site(tree, method, ["in_bw"])


# ---------------------------------------------------------------------------
# Hook-template Phase 2: hook_signature_str accepts template_params
# ---------------------------------------------------------------------------

def test_hook_signature_str_without_template_params_unchanged():
    """Default behaviour (no template_params) still emits symbolic WORD_BW."""
    from waveflow.build.hwgen import hook_signature_str
    sig = hook_signature_str(_hook_with_stream)
    assert sig.startswith("void _hook_with_stream(")
    assert "axi4s_word<WORD_BW>" in sig
    assert "template <" not in sig


def test_hook_signature_str_with_one_template_param():
    from waveflow.build.hwgen import hook_signature_str
    sig = hook_signature_str(_hook_with_stream, template_params=["in_bw"])
    assert sig.startswith("template <int in_bw>\n")
    assert "axi4s_word<in_bw>" in sig
    assert "axi4s_word<WORD_BW>" not in sig


def test_hook_signature_str_with_two_template_params():
    """Two stream args, two template params — substituted in order."""
    from waveflow.build.hwgen import hook_signature_str
    sig = hook_signature_str(
        _hook_two_streams, template_params=["in_bw", "out_bw"],
    )
    assert sig.startswith("template <int in_bw, int out_bw>\n")
    assert "axi4s_word<in_bw>" in sig
    assert "axi4s_word<out_bw>" in sig
    assert "WORD_BW" not in sig


def test_validate_single_call_site_inconsistent_raises():
    from waveflow.build.hwcodegen import SynthesisError
    from waveflow.build.hwgen import _validate_single_call_site
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.hwstmt import FunctionStmt, SeqStmt
    method = _FakeMethod("h")
    e1 = _stream_endpoint("a", HwParamValue(32, "in_bw"))
    e2 = _stream_endpoint("b", HwParamValue(64, "out_bw"))
    tree = SeqStmt(stmts=[
        FunctionStmt(method=method, inputs=[e1], outputs=[]),
        FunctionStmt(method=method, inputs=[e2], outputs=[]),
    ])
    with pytest.raises(SynthesisError, match="inconsistent"):
        _validate_single_call_site(tree, method, ["in_bw"])


# ---------------------------------------------------------------------------
# Hook-template Phase 3: header_to_cpp emits templated decls + .tpp includes
# ---------------------------------------------------------------------------

def test_header_to_cpp_non_templated_hook_no_tpp_include():
    """DemoComponent's process hook (no stream args) keeps the existing shape."""
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert "ap_uint<8> process(DemoCmdHdr cmd);" in hpp
    # No tpp include for a non-templated hook.
    assert "_impl.tpp" not in hpp
    # No template prefix either.
    assert "template <" not in hpp.split("namespace demo_impl {")[1]


def test_header_to_cpp_templated_hook_emits_template_and_tpp_include():
    """A hook that takes a HwParam-driven stream endpoint gets templated."""
    from waveflow.build.hwgen import header_to_cpp

    comp = _TmplComp(name="tcomp", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    # Inside the namespace block: template prefix + templated arg type.
    assert "template <int in_bw>" in hpp
    assert "axi4s_word<in_bw>" in hpp
    # .tpp include at the bottom for this hook.
    assert '#include "tcomp_process_impl.tpp"' in hpp
    # The include must come AFTER the templated decl.
    assert hpp.index("template <int in_bw>") < hpp.index("tcomp_process_impl.tpp")


# ---------------------------------------------------------------------------
# Hook-template Phase 4: impl_stub_to_tpp + kernel_files_to_str routing
# ---------------------------------------------------------------------------

def test_impl_stub_to_tpp_substrings():
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import (
        _collect_hooks_with_params,
        impl_stub_to_tpp,
    )

    comp = _TmplComp(name="tcomp", sim=Simulation())
    tree = extract_kernel(comp)
    hooks = _collect_hooks_with_params(tree)
    assert len(hooks) == 1
    hook, tparams = hooks[0]
    assert tparams == ["in_bw"]

    stub = impl_stub_to_tpp(comp, hook, tparams)
    for sub in [
        "// This file is included from tcomp.hpp",
        "template <int in_bw>",
        "ap_uint<8> process(",
        "axi4s_word<in_bw>",
        "// TODO: implement process",
        "return ap_uint<8>(0);",
        "namespace tcomp_impl {",
    ]:
        assert sub in stub, f"Missing substring: {sub!r}\n--- stub ---\n{stub}"


def test_impl_stub_to_tpp_does_not_include_header():
    """The .tpp is included from the .hpp, so it must NOT include the .hpp itself."""
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import (
        _collect_hooks_with_params,
        impl_stub_to_tpp,
    )

    comp = _TmplComp(name="tcomp", sim=Simulation())
    tree = extract_kernel(comp)
    hook, tparams = _collect_hooks_with_params(tree)[0]
    stub = impl_stub_to_tpp(comp, hook, tparams)
    assert '#include "tcomp.hpp"' not in stub


def test_kernel_files_to_str_uses_tpp_for_templated_hook():
    from waveflow.build.hwgen import kernel_files_to_str

    comp = _TmplComp(name="tcomp", sim=Simulation())
    files = kernel_files_to_str(type(comp))
    assert "tcomp.hpp" in files
    assert "tcomp.cpp" in files
    # Templated hook → .tpp, NOT .cpp.
    assert "tcomp_process_impl.tpp" in files
    assert "tcomp_process_impl.cpp" not in files


def test_kernel_files_to_str_uses_cpp_for_non_templated_hook():
    """DemoComponent's process hook has no stream args, so still .cpp."""
    from waveflow.build.hwgen import kernel_files_to_str
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    files = kernel_files_to_str(type(comp))
    assert "demo_process_impl.cpp" in files
    assert "demo_process_impl.tpp" not in files



# ---------------------------------------------------------------------------
# Phase 3: cpp_type with cpp_storage and kernel_signature for raw arrays
# ---------------------------------------------------------------------------

def test_cpp_type_dataarray_struct_storage_returns_class_name():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    cls = DataArray.specialize(element_type=Float32, max_shape=(4,), static=True, cpp_storage="struct")
    result = cpp_type(cls)
    assert result == cls.cpp_class_name()
    assert "float" not in result  # should use struct name, not raw


def test_cpp_type_dataarray_raw_storage_returns_c_array():
    from waveflow.build.hwgen import cpp_type
    from waveflow.hw.dataschema import DataArray, FloatField
    Float32 = FloatField.specialize(bitwidth=32)
    cls = DataArray.specialize(element_type=Float32, max_shape=(4,), static=True, cpp_storage="raw")
    result = cpp_type(cls)
    assert result == "float[4]"


def test_kernel_signature_raw_array_regmap_field():
    """A regmap field with cpp_storage='raw' emits '<elem> <name>[<count>]' in signature."""
    from waveflow.build.hwgen import kernel_signature
    from waveflow.hw.dataschema import DataArray, FloatField
    from waveflow.hw.hw_component import HwComponent
    from waveflow.hw.regmap import Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
    from waveflow.simulation.simulation import Simulation

    Float32 = FloatField.specialize(bitwidth=32)
    CoeffArrayRaw = DataArray.specialize(
        element_type=Float32, max_shape=(4,), static=True, cpp_storage="raw"
    )

    class _RawArrayComp(HwComponent):
        def __post_init__(self):
            super().__post_init__()
            self.regmap = VitisRegMap({
                "coeffs": RegField(CoeffArrayRaw, RegAccess.RW, description="Coefficients"),
            })
            self.s_lite = VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=self.on_start,
            )
            self.add_endpoint(self.s_lite)

        def on_start(self):
            return

    sim = Simulation()
    comp = _RawArrayComp(name="raw_comp", sim=sim)
    sig = kernel_signature(comp)
    assert "float coeffs[4]" in sig
    assert "CoeffArray& coeffs" not in sig


# ---------------------------------------------------------------------------
# Phase-12a Phase 2: _collect_schemas walks hook signatures
# ---------------------------------------------------------------------------

_TxIdField = _IntField.specialize(bitwidth=16, signed=False)


class _ExtraHdr(_DataList):
    """A DataList only ever referenced via a hook arg annotation."""
    elements = {
        "tx_id": {"schema": _TxIdField},
    }


@_synthesizable
def _hook_with_schema_arg(self, hdr: _ExtraHdr) -> _ProcessGen[None]:
    yield None


@_synthesizable
def _hook_returning_schema(self, x: _DemoCmdHdr) -> _ProcessGen[_ExtraHdr]:
    yield None
    return _ExtraHdr()


@_synthesizable
def _hook_stream_only(self, s_in: _StreamIFSlave) -> _ProcessGen[None]:
    yield None


@_synthesizable
def _hook_mixed_args(
    self,
    cmd: _DemoCmdHdr,
    s_in: _StreamIFSlave,
    hdr: _ExtraHdr,
) -> _ProcessGen[None]:
    yield None


def _seq_with_hook(method):
    from waveflow.hw.hwstmt import FunctionStmt, SeqStmt
    return SeqStmt(stmts=[
        FunctionStmt(method=method, inputs=[], outputs=[]),
    ])


def _bare_comp():
    return HwComponent(name="c", sim=Simulation())


def test_collect_schemas_picks_up_hook_arg_schema():
    from waveflow.build.hwgen import _collect_schemas
    schemas = _collect_schemas(_seq_with_hook(_hook_with_schema_arg), _bare_comp())
    assert _ExtraHdr in schemas


def test_collect_schemas_unwraps_processgen_return():
    from waveflow.build.hwgen import _collect_schemas
    schemas = _collect_schemas(_seq_with_hook(_hook_returning_schema), _bare_comp())
    assert _DemoCmdHdr in schemas
    assert _ExtraHdr in schemas


def test_collect_schemas_skips_stream_args():
    from waveflow.build.hwgen import _collect_schemas
    schemas = _collect_schemas(_seq_with_hook(_hook_stream_only), _bare_comp())
    assert _DemoCmdHdr not in schemas
    assert _ExtraHdr not in schemas


def test_collect_schemas_mixed_hook_args():
    from waveflow.build.hwgen import _collect_schemas
    schemas = _collect_schemas(_seq_with_hook(_hook_mixed_args), _bare_comp())
    assert _DemoCmdHdr in schemas
    assert _ExtraHdr in schemas


# ---------------------------------------------------------------------------
# Phase-12a Phase 3: header_to_cpp emits utility includes
# ---------------------------------------------------------------------------

def test_header_no_array_schemas_emits_no_utility_includes():
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    # DemoComponent has no DataArray fields → no array_utils include line.
    assert "_array_utils.h" not in hpp


def test_header_data_array_field_emits_utility_include():
    """A component with a DataArray[Float32] regmap field gets the include line."""
    from dataclasses import dataclass

    from waveflow.build.hwgen import header_to_cpp
    from waveflow.hw.dataschema import DataArray, FloatField
    from waveflow.hw.regmap import (
        Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave,
    )
    from waveflow.simulation.simobj import ProcessGen

    Float32 = FloatField.specialize(bitwidth=32, include_dir="include")

    class CoeffsArr(DataArray):
        element_type = Float32
        static = True
        max_shape = (4,)

    @dataclass
    class _ArrComp(HwComponent):
        cpp_kernel_name: ClassVar[str | None] = "arrk"
        in_bw: HwParam[int] = 32

        def __post_init__(self):
            super().__post_init__()
            self.s_in = _StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=self.in_bw,
            )
            self.regmap = VitisRegMap({
                "halted": RegField(Bit, RegAccess.R),
                "coeffs": RegField(CoeffsArr, RegAccess.RW),
            })
            self.s_lite = VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=self.on_start,
            )
            for ep in (self.s_in, self.s_lite):
                self.add_endpoint(ep)

        def on_start(self) -> ProcessGen[None]:
            while True:
                return

    comp = _ArrComp(name="arr", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert '#include "include/float32_array_utils.h"' in hpp


def test_header_utility_include_deduped():
    """Same array referenced from regmap and a hook arg: include appears once."""
    from waveflow.build.hwgen import _collect_utility_includes
    from waveflow.hw.dataschema import DataArray, FloatField

    Float32 = FloatField.specialize(bitwidth=32, include_dir="include")

    class Float32A(DataArray):
        element_type = Float32
        static = True
        max_shape = (4,)

    # Pass the same schema twice; expect a single include path back.
    paths = _collect_utility_includes([Float32A, Float32A])
    assert paths == ["include/float32_array_utils.h"]


# ---------------------------------------------------------------------------
# Conditional streamutils_hls.h include (regmap-only vs stream-using)
# ---------------------------------------------------------------------------

def test_header_omits_streamutils_for_regmap_only_component():
    """A regmap-only component (no StreamIF endpoints) must not include
    streamutils_hls.h. The user shouldn't have to register a step to produce
    a header their kernel doesn't use.
    """
    from waveflow.build.hwgen import header_to_cpp

    Int32 = _IntField.specialize(bitwidth=32, signed=True)

    @_dataclass
    class _RegMapOnly(HwComponent):
        cpp_kernel_name: ClassVar[str | None] = "rmonly"

        def __post_init__(self) -> None:
            super().__post_init__()
            self.regmap = _VitisRegMap({
                "x": _RegField(Int32, _RegAccess.RW),
                "y": _RegField(Int32, _RegAccess.R),
            })
            self.s_lite = _VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=self.on_start,
            )
            self.add_endpoint(self.s_lite)

        def on_start(self) -> _ProcessGen[None]:
            x = self.regmap.get("x")
            self.regmap.set("y", x)

    comp = _RegMapOnly(name="rmonly", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert 'streamutils_hls.h' not in hpp, (
        "regmap-only kernels must not emit the streamutils include:\n" + hpp
    )


def test_header_includes_streamutils_for_stream_using_component():
    """A component with StreamIF endpoints (poly's shape) still gets the
    streamutils_hls.h include — guards against regressing the fix.
    """
    from waveflow.build.hwgen import header_to_cpp
    from tests.hw.test_resolve import DemoComponent

    comp = DemoComponent(name="demo", sim=Simulation())
    hpp = header_to_cpp(type(comp))
    assert '#include "include/streamutils_hls.h"' in hpp


# ---------------------------------------------------------------------------
# Inline regmap.get(...) + local-shadows-regmap-field fix
# ---------------------------------------------------------------------------

from waveflow.hw.synth import synthesizable as _synth2

_S32_PH3 = _IntField.specialize(bitwidth=32, signed=True)


@_dataclass
class _InlineGetComp(HwComponent):
    """``on_start`` reads regmap fields inline as a call argument, and
    assigns the compute result into a local whose name matches the
    output regmap field. Both patterns must lower cleanly to C++.
    """

    cpp_kernel_name: ClassVar[str | None] = "inline_get"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = _VitisRegMap({
            "x": _RegField(_S32_PH3, _RegAccess.RW),
            "a": _RegField(_S32_PH3, _RegAccess.RW),
            "b": _RegField(_S32_PH3, _RegAccess.RW),
            "y": _RegField(_S32_PH3, _RegAccess.R),
        })
        self.s_lite = _VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        self.add_endpoint(self.s_lite)

    def on_start(self) -> _ProcessGen[None]:
        y = self.compute(
            self.regmap.get("x"),
            self.regmap.get("a"),
            self.regmap.get("b"),
        )
        self.regmap.set("y", y)

    @_synth2
    def compute(self, x: _S32_PH3, a: _S32_PH3, b: _S32_PH3) -> _S32_PH3:
        return x


def test_inline_regmap_get_lowers_to_kernel_param_name():
    """``self.regmap.get("x")`` as a call argument lowers to the kernel
    parameter ``x``, not raw AST repr text.
    """
    from waveflow.build.hwgen import kernel_to_cpp

    cpp = kernel_to_cpp(_InlineGetComp)
    # The compute call should reference the kernel parameter names.
    assert "compute(x, a, b)" in cpp, (
        "expected inline regmap.get(...) to lower to bare param names:\n"
        + cpp
    )
    # The bug symptom — ast.dump text — must not appear anywhere.
    assert "Call(func=" not in cpp
    assert "Attribute(value=" not in cpp


def test_local_shadowing_regmap_field_does_not_self_assign():
    """``y = compute(...); self.regmap.set("y", y)`` must not lower to the
    no-op ``y = y;``. The local that aliases the kernel parameter is
    either renamed at codegen time (option a) or extraction raises a
    clear error (option b).
    """
    from waveflow.build.hwcodegen import SynthesisError
    from waveflow.build.hwgen import kernel_to_cpp

    try:
        cpp = kernel_to_cpp(_InlineGetComp)
    except SynthesisError as exc:
        # Option (b) fallback: a clear error pointing at the shadowing.
        msg = str(exc).lower()
        assert "y" in msg and ("shadow" in msg or "rename" in msg), (
            f"SynthesisError did not explain the shadowing issue: {exc}"
        )
        return

    # Option (a) success path: the assignment is no longer a self-assign.
    assert "y = y;" not in cpp, (
        "regmap.set('y', y) emitted as self-assignment; the compute "
        "result is being lost:\n" + cpp
    )
    # The compute result must flow into the kernel-parameter ``y`` via
    # SOME renamed local.
    assert "compute(x, a, b)" in cpp
    # Final assignment line of the form ``y = <something_not_y>;`` must
    # exist so the parameter gets the compute result.
    import re
    final_assigns = re.findall(r"^\s*y\s*=\s*([A-Za-z_]\w*)\s*;", cpp, re.M)
    assert any(rhs != "y" for rhs in final_assigns), (
        "no ``y = <local>;`` assignment found that propagates the compute "
        "result:\n" + cpp
    )

