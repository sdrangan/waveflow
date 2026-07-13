"""Walk a resolved ``HwStmt`` tree and emit C++ source as a string."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from waveflow.hw.dataschema import DataArray, DataSchema, EnumField, FloatField, IntField
from waveflow.hw.hwstmt import (
    CaseStmt,
    ContinueStmt,
    DutBindStmt,
    FieldRef,
    FunctionStmt,
    HwStmt,
    HwVar,
    KernelCallStmt,
    MMArrayReadStmt,
    MMArrayWriteStmt,
    MemAllocArrayStmt,
    MemBindStmt,
    MemReadArrayStmt,
    Ref,
    ReturnStmt,
    SchemaBindStmt,
    SeqStmt,
    TbFileIOStmt,
    TbRegmapFileReadStmt,
    TbStatusJsonStmt,
    TbStreamIOStmt,
    WhileStmt,
)
from waveflow.hw.aximm_queue import AXIMMQueueGetStmt
from waveflow.hw.interface import (
    StreamDrainStmt,
    StreamGetStmt,
    StreamWriteStmt,
)
from waveflow.hw.memif import PollUntilStmt
from waveflow.hw.regmap import RegMapGetStmt, RegMapSetStmt

if TYPE_CHECKING:
    from waveflow.hw.hw_component import HwComponent


@dataclass
class CodegenCtx:
    comp: HwComponent
    params: dict[str, str] = field(default_factory=dict)
    endpoint_names: dict[int, str] = field(default_factory=dict)
    indent: int = 1

    def pad(self) -> str:
        return "    " * self.indent

    def child(self) -> CodegenCtx:
        return CodegenCtx(
            comp=self.comp,
            params=self.params,
            endpoint_names=self.endpoint_names,
            indent=self.indent + 1,
        )


def to_cpp(stmt: HwStmt, ctx: CodegenCtx) -> str:
    """Emit C++ source for a statement (and its children). Returns a string."""
    if isinstance(stmt, WhileStmt):
        return _emit_while(stmt, ctx)
    if isinstance(stmt, SeqStmt):
        return _emit_seq(stmt, ctx)
    if isinstance(stmt, ReturnStmt):
        return _emit_return(stmt, ctx)
    if isinstance(stmt, ContinueStmt):
        return f"{ctx.pad()}continue;"
    if isinstance(stmt, CaseStmt):
        return _emit_case(stmt, ctx)
    if isinstance(stmt, AXIMMQueueGetStmt):
        return _emit_aximm_queue_get(stmt, ctx)
    if isinstance(stmt, PollUntilStmt):
        return _emit_poll_until(stmt, ctx)
    if isinstance(stmt, StreamGetStmt):
        return _emit_stream_get(stmt, ctx)
    if isinstance(stmt, StreamWriteStmt):
        return _emit_stream_write(stmt, ctx)
    if isinstance(stmt, StreamDrainStmt):
        return _emit_stream_drain(stmt, ctx)
    if isinstance(stmt, MMArrayReadStmt):
        return _emit_mm_array_read(stmt, ctx)
    if isinstance(stmt, MMArrayWriteStmt):
        return _emit_mm_array_write(stmt, ctx)
    if isinstance(stmt, RegMapGetStmt):
        return _emit_regmap_get(stmt, ctx)
    if isinstance(stmt, RegMapSetStmt):
        return _emit_regmap_set(stmt, ctx)
    if isinstance(stmt, FunctionStmt):
        return _emit_function_call(stmt, ctx)
    raise NotImplementedError(
        f"Codegen for {type(stmt).__name__} not implemented yet"
    )


def _emit_while(stmt: WhileStmt, ctx: CodegenCtx) -> str:
    body = to_cpp(stmt.body, ctx.child())
    return f"{ctx.pad()}while (true) {{\n{body}\n{ctx.pad()}}}"


def _emit_seq(stmt: SeqStmt, ctx: CodegenCtx) -> str:
    return "\n".join(to_cpp(child, ctx) for child in stmt.stmts)


def _emit_return(stmt: ReturnStmt, ctx: CodegenCtx) -> str:
    if stmt.value is None:
        return f"{ctx.pad()}return;"
    return f"{ctx.pad()}return {_emit_expr(stmt.value, ctx)};"


def _emit_case(stmt: CaseStmt, ctx: CodegenCtx) -> str:
    lhs = stmt.var.name if stmt.field is None else f"{stmt.var.name}.{stmt.field}"
    # The rhs may be a constant (enum / literal) OR a runtime variable in scope
    # (a HwVar / FieldRef — e.g. the ring-poll's ``tail != head``).  _emit_expr
    # emits the variable name for those and the C++ literal for constants.
    rhs = _emit_expr(stmt.value, ctx)
    # When the bare variable is itself an IntEnum (e.g. evaluate's PolyError
    # return), the local var is declared as ap_uint<8> (matching the hook's
    # actual C++ return type).  Cast the enum literal so the comparison
    # compiles against the ap_uint<8> LHS.
    if (stmt.field is None
            and isinstance(stmt.var.typ, type)
            and issubclass(stmt.var.typ, IntEnum)
            and isinstance(stmt.value, IntEnum)):
        rhs = f"(ap_uint<8>)static_cast<unsigned int>({rhs})"
    cond = f"{lhs} {stmt.op} {rhs}"
    lines = [f"{ctx.pad()}if ({cond}) {{"]
    lines.append(to_cpp(stmt.if_true, ctx.child()))
    lines.append(f"{ctx.pad()}}}")
    if stmt.if_false is not None:
        lines[-1] = f"{ctx.pad()}}} else {{"
        lines.append(to_cpp(stmt.if_false, ctx.child()))
        lines.append(f"{ctx.pad()}}}")
    return "\n".join(lines)


def _emit_expr(expr, ctx: CodegenCtx) -> str:
    from waveflow.hw.hw_component import HwParamValue
    if isinstance(expr, HwVar):
        return expr.name
    if isinstance(expr, Ref):
        return expr.var.name
    if isinstance(expr, FieldRef):
        return f"{expr.var.name}.{expr.field}"
    # A HwParam read (e.g. ``self.max_ndata``) lowers to its named C++ constant
    # (emitted in the kernel header), not its inlined value — must precede the
    # int branch in _emit_literal since HwParamValue is an int subclass.
    if isinstance(expr, HwParamValue):
        return expr.param_name
    import ast as _ast
    if isinstance(expr, _ast.AST):
        return _emit_ast_expr(expr, ctx)
    return _emit_literal(expr)


def _emit_ast_expr(node, ctx: CodegenCtx) -> str:
    """Lower a small AST sub-expression (an unresolved call arg) to C++.

    Handles ``cmd.field`` attribute access, integer constants, and ``+``/``-``/
    ``*`` arithmetic (e.g. the ``nbins - 1`` edge count)."""
    import ast as _ast
    if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, _ast.Name):
        return node.id
    if isinstance(node, _ast.Constant):
        return _emit_literal(node.value)
    if isinstance(node, _ast.BinOp):
        op = {_ast.Add: '+', _ast.Sub: '-', _ast.Mult: '*'}.get(type(node.op))
        if op is not None:
            return (f"{_emit_ast_expr(node.left, ctx)} {op} "
                    f"{_emit_ast_expr(node.right, ctx)}")
    from waveflow.build.hwcodegen import SynthesisError
    raise SynthesisError(f"Unsupported expression in m_axi argument: {_ast.dump(node)}")


def _emit_stream_get(stmt: StreamGetStmt, ctx: CodegenCtx) -> str:
    # stmt.inputs = [schema_class]; stmt.outputs = [HwVar]
    schema_cls = stmt.inputs[0]
    out = stmt.outputs[0]
    cpp_type = schema_cls.cpp_class_name()
    ep = stmt.method.__self__  # type: ignore[attr-defined]
    stream_name = _endpoint_name(ep, ctx)
    tmpl = _stream_template_arg(ep)
    pad = ctx.pad()
    return (
        f"{pad}{cpp_type} {out.name};\n"
        f"{pad}{out.name}.read_axi4_stream<{tmpl}>({stream_name});"
    )


def _emit_aximm_queue_get(stmt: AXIMMQueueGetStmt, ctx: CodegenCtx) -> str:
    """``cmd = self.cmd_queue.get(self.Cmd)`` → declare the command local and call the
    hand-written ring-dequeue hook (``aximm_queue_impl::queue_get``).

    Strategy A (decision D5): the dequeue is a hand-written C++ hook, mirroring the
    ``vmac_compute_impl.tpp`` pattern, rather than inlined codegen.  The ring geometry
    (base / capacity / elem_words) is fixed by the Python :class:`AXIMMQueueLayout` and
    passed as template params; the m_axi pointer is the queue master's bound port (the
    same ``gmem`` the datapath uses).  ``inputs[0]`` is the command schema class;
    ``outputs[0]`` is the bound local."""
    schema_cls = stmt.inputs[0]
    out = stmt.outputs[0]
    cpp_t = schema_cls.cpp_class_name()
    queue = stmt.method.__self__  # type: ignore[attr-defined]  # the AXIMMQueue
    layout = queue.layout
    port_name = _endpoint_name(queue.master, ctx)
    bw = int(layout.mem_bw)
    pad = ctx.pad()
    return (
        f"{pad}{cpp_t} {out.name};\n"
        f"{pad}aximm_queue_impl::queue_get<{cpp_t}, {bw}, "
        f"{layout.base_addr}, {layout.capacity}, {layout.elem_words}>"
        f"({port_name}, {out.name});"
    )


def _emit_poll_until(stmt: PollUntilStmt, ctx: CodegenCtx) -> str:
    """``val = self.m_mem.poll_until(addr, Ne(head), poll_interval)`` → a blocking
    poll over the master's m_axi pointer, lowered to the reusable ring-poll
    primitive ``poll_until_impl::poll_until_{eq,ne}`` (the C++ twin of the sim
    ``MMIFMaster.poll_until``).

    ``inputs[0]`` is the watched byte address; ``inputs[1]`` is the
    :class:`~waveflow.hw.memif.LoweredPollCond` (op + const-or-HwVar rhs).
    ``poll_interval`` (``inputs[2]``) is sim-only (the AT-model discovery delay)
    and is NOT emitted — a hardware poll just spins on the word."""
    master = stmt.method.__self__  # type: ignore[attr-defined]  # the MMIFMaster
    port_name = _endpoint_name(master, ctx)
    bw = int(master.bitwidth)
    out = stmt.outputs[0]
    cond = stmt.cond
    assert cond is not None, "PollUntilStmt requires a lowered PollCond (op + rhs)"
    addr_expr = _emit_expr(stmt.addr_expr, ctx)
    rhs = _emit_expr(cond.rhs, ctx)
    fn = "poll_until_eq" if cond.op == "==" else "poll_until_ne"
    out_t = f"ap_uint<{bw}>"
    word = f"memmgr::byte_addr_to_word_index<{bw}>({addr_expr})"
    pad = ctx.pad()
    return (
        f"{pad}{out_t} {out.name} = poll_until_impl::{fn}<{bw}>("
        f"{port_name}, {word}, ({out_t}){rhs});"
    )


def _emit_stream_write(stmt: StreamWriteStmt, ctx: CodegenCtx) -> str:
    # stmt.inputs = [HwVar of the value to write]
    value = stmt.inputs[0]
    ep = stmt.method.__self__  # type: ignore[attr-defined]
    stream_name = _endpoint_name(ep, ctx)
    tmpl = _stream_template_arg(ep)
    pad = ctx.pad()
    return f"{pad}{value.name}.write_axi4_stream<{tmpl}>({stream_name}, true);"


def _emit_stream_drain(stmt: StreamDrainStmt, ctx: CodegenCtx) -> str:
    ep = stmt.method.__self__  # type: ignore[attr-defined]
    stream_name = _endpoint_name(ep, ctx)
    tmpl = _stream_template_arg(ep)
    pad = ctx.pad()
    return f"{pad}streamutils::flush_axi4_stream_to_tlast<{tmpl}>({stream_name});"


def _mm_buffer_max(comp) -> int:
    """Compile-time max element count for an m_axi buffered read (decision 5).

    The static local buffer the kernel reads into needs a fixed size; it comes
    from a ``max_n`` ``HwParam`` bound on the component.  Fail loudly when no
    such bound exists rather than emit an unsized array.
    """
    val = getattr(comp, 'max_n', None)
    if val is None:
        from waveflow.build.hwcodegen import SynthesisError
        raise SynthesisError(
            f"{type(comp).__name__}: an m_axi buffered read needs a compile-time "
            f"max buffer size, but no 'max_n' HwParam is declared "
            f"(see plans/aximm_codegen.md decision 5)."
        )
    return int(val)


def _mm_buffer_bound(stmt, buf: str, ctx: CodegenCtx) -> str:
    """Compile-time size of the static local buffer an m_axi read fills.

    Comes from the read's explicit ``max_count=`` argument (decisions 3 & 5):
    each buffer declares its own bound, so a kernel can read several buffers of
    different sizes into the same bundle.  Fail loudly when it is missing rather
    than emit an unsized array — there is no global ``max_n`` fallback.
    """
    if stmt.max_expr is None:
        from waveflow.build.hwcodegen import SynthesisError
        raise SynthesisError(
            f"m_axi read into '{buf}' has no compile-time buffer bound; pass an "
            f"explicit max_count= (e.g. read_array(ElemT, n, addr, "
            f"max_count=self.max_ndata))."
        )
    return _emit_expr(stmt.max_expr, ctx)


def _emit_mm_array_read(stmt: MMArrayReadStmt, ctx: CodegenCtx) -> str:
    """``buf = port.read_array(ElemT, count, addr)`` →
    a static local buffer + an ``<elem>_array_utils::read_array_slice`` burst
    over the resident range ``[0, count)`` (mirrors the generated histogram kernel)."""
    port_name = _endpoint_name(stmt.port, ctx)
    bw = int(stmt.port.bitwidth)
    ns = _array_utils_ns(stmt.elem_type)
    elem_cpp = cpp_type(stmt.elem_type)
    buf = stmt.target_var.name
    bufmax = _mm_buffer_bound(stmt, buf, ctx)
    count = _emit_expr(stmt.count_expr, ctx)
    addr = _emit_expr(stmt.addr_expr, ctx)
    pad = ctx.pad()
    return (
        f"{pad}static {elem_cpp} {buf}[{bufmax}];\n"
        f"{pad}{ns}::read_array_slice<{bw}>("
        f"{port_name} + memmgr::byte_addr_to_word_index<{bw}>({addr}), "
        f"0, {count}, {buf});"
    )


def _emit_mm_array_write(stmt: MMArrayWriteStmt, ctx: CodegenCtx) -> str:
    """``port.write_array(buf, ElemT, addr, count)`` →
    the dual ``<elem>_array_utils::write_array_slice`` burst over ``[0, count)``."""
    port_name = _endpoint_name(stmt.port, ctx)
    bw = int(stmt.port.bitwidth)
    ns = _array_utils_ns(stmt.elem_type)
    src = _emit_expr(stmt.source_expr, ctx)
    count = _emit_expr(stmt.count_expr, ctx)
    addr = _emit_expr(stmt.addr_expr, ctx)
    pad = ctx.pad()
    return (
        f"{pad}{ns}::write_array_slice<{bw}>("
        f"{src}, {port_name} + memmgr::byte_addr_to_word_index<{bw}>({addr}), "
        f"0, {count});"
    )


def _tree_has_poll_until(tree: HwStmt) -> bool:
    """True if any ``PollUntilStmt`` is reachable from ``tree`` (so the header
    must pull in the reusable ring-poll primitive)."""
    found = False

    def walk(node: HwStmt) -> None:
        nonlocal found
        if isinstance(node, PollUntilStmt):
            found = True
        if isinstance(node, SeqStmt):
            for s in node.stmts:
                walk(s)
        elif isinstance(node, WhileStmt):
            walk(node.body)
        elif isinstance(node, CaseStmt):
            walk(node.if_true)
            if node.if_false is not None:
                walk(node.if_false)

    walk(tree)
    return found


def _collect_mm_stmts(tree: HwStmt) -> list:
    """All ``MMArrayRead/WriteStmt`` reachable from ``tree``, in order."""
    out: list = []

    def walk(node: HwStmt) -> None:
        if isinstance(node, (MMArrayReadStmt, MMArrayWriteStmt)):
            out.append(node)
        if isinstance(node, SeqStmt):
            for s in node.stmts:
                walk(s)
        elif isinstance(node, WhileStmt):
            walk(node.body)
        elif isinstance(node, CaseStmt):
            walk(node.if_true)
            if node.if_false is not None:
                walk(node.if_false)

    walk(tree)
    return out


def _kernel_hwparam_constants(tree: HwStmt) -> dict[str, int]:
    """Ordered ``{param_name: value}`` for every HwParam read in ``tree`` (e.g.
    the ``max_count`` bounds), emitted as ``static const int`` in the header so
    the generated buffers / pragmas reference named constants, not inlined ints.
    """
    from waveflow.hw.hw_component import HwParamValue
    consts: dict[str, int] = {}
    for s in _collect_mm_stmts(tree):
        mx = s.max_expr
        if isinstance(mx, HwParamValue):
            consts.setdefault(mx.param_name, int(mx))
    return consts


def _mm_port_depth(port_ep, tree: HwStmt, ctx: CodegenCtx) -> str:
    """C++ expression for an m_axi port's ``depth`` — the sum of the buffer
    bounds of every array op on that port (the total region the master sees)."""
    maxes = [
        _emit_expr(s.max_expr, ctx)
        for s in _collect_mm_stmts(tree)
        if s.port is port_ep and s.max_expr is not None
    ]
    return " + ".join(maxes) if maxes else "1"


def _discover_mm_masters(comp) -> list[tuple[str, object]]:
    """Return ``[(attr_name, endpoint)]`` for each ``MMIFMaster`` on ``comp``."""
    from waveflow.hw.memif import MMIFMaster
    return [
        (name, val) for name, val in vars(comp).items()
        if isinstance(val, MMIFMaster)
    ]


def _collect_mm_elem_types(tree: HwStmt) -> list[type]:
    """Ordered, deduplicated element types used by m_axi reads/writes in ``tree``."""
    out: list[type] = []
    seen: set[int] = set()

    def walk(node: HwStmt) -> None:
        if isinstance(node, (MMArrayReadStmt, MMArrayWriteStmt)):
            et = node.elem_type
            if et is not None and id(et) not in seen:
                seen.add(id(et))
                out.append(et)
        if isinstance(node, SeqStmt):
            for s in node.stmts:
                walk(s)
        elif isinstance(node, WhileStmt):
            walk(node.body)
        elif isinstance(node, CaseStmt):
            walk(node.if_true)
            if node.if_false is not None:
                walk(node.if_false)

    walk(tree)
    return out


def _endpoint_name(endpoint, ctx: CodegenCtx) -> str:
    """Find the Python attribute name on ``ctx.comp`` that this endpoint is bound to."""
    key = id(endpoint)
    if key in ctx.endpoint_names:
        return ctx.endpoint_names[key]
    for name, val in vars(ctx.comp).items():
        if val is endpoint:
            ctx.endpoint_names[key] = name
            return name
    raise RuntimeError(
        f"Endpoint {endpoint!r} not found on component {type(ctx.comp).__name__}"
    )


def _emit_regmap_get(stmt: RegMapGetStmt, ctx: CodegenCtx) -> str:
    # stmt.inputs = [field_name str]; stmt.outputs = [HwVar]
    field_name = stmt.inputs[0]
    out = stmt.outputs[0]
    # When the bound HwVar name equals the field name, the kernel-signature
    # parameter (s_axilite scalar/array) is already in scope under that
    # exact name, so no copy/binding is needed (and emitting one trips
    # self-init for raw-array fields).  Emit a comment so the generated
    # source still records the read intent.
    if out.name == field_name:
        return f"{ctx.pad()}// {out.name} is already in scope via the kernel signature."
    cpp_t = (
        out.typ.cpp_class_name()
        if hasattr(out.typ, 'cpp_class_name')
        else 'auto'
    )
    return f"{ctx.pad()}{cpp_t} {out.name} = {field_name};"


def _emit_regmap_set(stmt: RegMapSetStmt, ctx: CodegenCtx) -> str:
    # stmt.inputs = [field_name str, value (HwVar | FieldRef | literal)]
    field_name = stmt.inputs[0]
    value = stmt.inputs[1]
    rhs = _emit_expr(value, ctx)
    return f"{ctx.pad()}{field_name} = {rhs};"


def _emit_function_call(stmt: FunctionStmt, ctx: CodegenCtx) -> str:
    """Emit a call to a ``@synthesizable`` user hook.

    The hook is a free function in C++ (no class wrapping); when a namespace
    is resolved for the component, the call site is explicitly qualified
    (``demo::process(...)``). Inputs become positional arguments; the
    return value (if any) is assigned to the output ``HwVar``, which is
    also declared inline.
    """
    if len(stmt.outputs) > 1:
        raise NotImplementedError(
            "Multi-output FunctionStmt (tuple return) is not supported"
        )
    func_name = stmt.method.__name__  # type: ignore[attr-defined]
    ns = resolved_namespace(type(ctx.comp))
    qualified = func_name if ns is None else f"{ns}::{func_name}"
    args = [_emit_call_arg(a, ctx) for a in stmt.inputs]
    arg_str = ", ".join(args)
    pad = ctx.pad()
    if not stmt.outputs:
        return f"{pad}{qualified}({arg_str});"
    out = stmt.outputs[0]
    # An array-typed output (a hook that "returns" a buffer) lowers to a static
    # local buffer the hook fills in place via an appended out-parameter (HLS
    # can't return arrays by value) — paired with the void+out-param signature.
    if (isinstance(out.typ, type) and issubclass(out.typ, DataArray)
            and getattr(out.typ, 'cpp_storage', 'struct') == 'raw'):
        elem_cpp = cpp_type(out.typ.element_type)
        size = out.typ._declared_count()
        return (
            f"{pad}static {elem_cpp} {out.name}[{size}];\n"
            f"{pad}{qualified}({arg_str}, {out.name});"
        )
    cpp_t = (
        out.typ.cpp_class_name()
        if hasattr(out.typ, 'cpp_class_name')
        else _cpp_type_for(out.typ)
    )
    return f"{pad}{cpp_t} {out.name} = {qualified}({arg_str});"


def _cpp_type_for(typ) -> str:
    """Fallback C++ type for ``HwVar.typ`` values without ``cpp_class_name``.

    ``IntEnum`` subclasses map to ``ap_uint<8>`` to match what the codegen
    uses everywhere else for enum return/arg types (see ``cpp_type``).  Using
    the enum class name here would mismatch the auto-generated hook forward
    declaration, which returns ``ap_uint<8>`` for ``ProcessGen[<IntEnum>]``
    annotations.
    """
    if typ is None:
        return 'auto'
    if isinstance(typ, type) and issubclass(typ, IntEnum):
        return 'ap_uint<8>'
    name = getattr(typ, '__name__', None)
    if name:
        return name
    return 'auto'


def _emit_call_arg(arg, ctx: CodegenCtx) -> str:
    """Emit one argument: HwVar -> name, endpoint -> attr name, literal -> literal."""
    if isinstance(arg, HwVar):
        return arg.name
    if isinstance(arg, FieldRef):
        return f"{arg.var.name}.{arg.field}"
    from waveflow.hw.interface import InterfaceEndpoint
    if isinstance(arg, InterfaceEndpoint):
        return _endpoint_name(arg, ctx)
    return _emit_literal(arg)


def _emit_literal(value) -> str:
    """Emit a Python value as a C++ literal."""
    from enum import IntEnum
    if isinstance(value, IntEnum):
        return f"{type(value).__name__}::{value.name}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    return repr(value)


def cpp_type(typ) -> str:
    """Translate a Python type or ``HwVar.typ`` marker to its HLS C++ representation.

    Rules (matching the kernel-files plan):

    - ``IntField`` (``signed=False``) → ``ap_uint<bw>``
    - ``IntField`` (``signed=True``)  → ``ap_int<bw>``
    - ``EnumField`` (and bare ``IntEnum`` subclasses) → ``ap_uint<8>`` (v1)
    - ``FloatField`` (bitwidth 32) → ``float``; (bitwidth 64) → ``double``
    - ``DataArray`` (``cpp_storage='raw'``) → ``<elem>[<count>]``
    - Other ``DataSchema`` subclasses → ``cls.cpp_class_name()``
    - ``None`` → ``RuntimeError`` (unresolved type leaked through)
    """
    if typ is None:
        raise RuntimeError("cpp_type called with None — unresolved HwVar leaked")
    if isinstance(typ, type) and issubclass(typ, DataSchema):
        if issubclass(typ, IntField):
            bw = typ.get_bitwidth()
            signed = getattr(typ, 'signed', False)
            return f"ap_int<{bw}>" if signed else f"ap_uint<{bw}>"
        if issubclass(typ, FloatField):
            bw = typ.get_bitwidth()
            if bw == 32:
                return "float"
            if bw == 64:
                return "double"
            raise RuntimeError(
                f"FloatField bitwidth {bw} not supported (32 or 64 only)"
            )
        if issubclass(typ, EnumField):
            return "ap_uint<8>"  # v1 simplification; widen later if needed
        if issubclass(typ, DataArray) and getattr(typ, 'cpp_storage', 'struct') == 'raw':
            elem_cpp = cpp_type(typ.element_type)
            return f"{elem_cpp}[{typ._declared_count()}]"
        return typ.cpp_class_name()
    # Bare IntEnum subclasses (e.g. resolved FunctionStmt return types) map the
    # same as EnumField. The plan explicitly bounds enums at 8 bits for v1.
    if isinstance(typ, type) and issubclass(typ, IntEnum):
        return "ap_uint<8>"
    # Plain Python scalars in hook annotations (e.g. a loop-count ``int``).
    if typ is int:
        return "int"
    if typ is bool:
        return "bool"
    if typ is float:
        return "float"
    raise RuntimeError(f"cpp_type: cannot translate {typ!r}")


def _validate_no_name_collisions(comp) -> None:
    """Raise if ``HwParam``, regmap-field, and endpoint-attr names overlap."""
    import sys
    import typing
    from waveflow.build.hwcodegen import SynthesisError
    from waveflow.hw.hw_component import HwParam

    hw_param_names: set[str] = set()
    for klass in type(comp).__mro__:
        for name, hint in getattr(klass, '__annotations__', {}).items():
            if isinstance(hint, str):
                mod = sys.modules.get(klass.__module__)
                globs: dict = vars(mod) if mod is not None else {}
                try:
                    hint = eval(hint, globs)  # noqa: S307
                except Exception:
                    continue
            if typing.get_origin(hint) is HwParam:
                hw_param_names.add(name)

    endpoint_attrs = {name for name, _ in _discover_stream_endpoints(comp)}
    regmap_field_names: set[str] = set()
    regmap_slave = _discover_regmap(comp)
    if regmap_slave is not None:
        regmap_field_names = set(regmap_slave.regmap._fields.keys())

    pairs = [
        ('HwParam', 'endpoint',     hw_param_names & endpoint_attrs),
        ('HwParam', 'regmap field', hw_param_names & regmap_field_names),
        ('endpoint', 'regmap field', endpoint_attrs & regmap_field_names),
    ]
    collisions = [(a, b, names) for a, b, names in pairs if names]
    if collisions:
        msg = "; ".join(
            f"{a} vs {b}: {sorted(n)}" for a, b, n in collisions
        )
        raise SynthesisError(
            f"Name collisions in component {type(comp).__name__}: {msg}"
        )


def _stream_template_arg(ep) -> str:
    """Return the template argument string for an endpoint's bitwidth.

    Always emits the literal integer value.  Top-level kernels are now
    concrete (no ``template <int ...>`` block); stream-type expressions
    use the concrete bitwidth from the variant's ``HwParamValue``.
    """
    return str(int(ep.bitwidth))


def _discover_stream_endpoints(comp) -> list[tuple[str, object]]:
    """Return ``[(attr_name, endpoint)]`` for each stream endpoint on ``comp``."""
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    result: list[tuple[str, object]] = []
    for name, val in vars(comp).items():
        if isinstance(val, (StreamIFSlave, StreamIFMaster)):
            result.append((name, val))
    return result


def _discover_regmap(comp):
    """Return the component's ``VitisRegMapMMIFSlave`` endpoint, or ``None``."""
    from waveflow.hw.regmap import VitisRegMapMMIFSlave
    for val in vars(comp).values():
        if isinstance(val, VitisRegMapMMIFSlave):
            return val
    return None


def kernel_signature(comp, variant_suffix: str = "") -> str:
    """Build the concrete kernel function signature + ``#pragma HLS INTERFACE`` lines.

    Returns a multi-line string ending with the opening ``{`` of the function
    body and the pragmas.  The caller appends the body and the closing ``}``.

    Top-level kernels are emitted as **concrete** (non-templated) functions so
    Vitis HLS RTL generation can attach the AXI interfaces — see the kernel
    variants plan.  Stream-type expressions in the signature use the literal
    integer bitwidths from ``ep.bitwidth`` (which for a variant-specific
    component instance carry the concrete ``HwParamValue`` for that variant).

    ``variant_suffix`` is appended to the function name as ``_{suffix}`` when
    non-empty, so additional ``param_supports`` variants get unique top-level
    names like ``poly_bw64``.
    """
    _validate_no_name_collisions(comp)
    base_name = cpp_kernel_name(type(comp))
    name = f"{base_name}_{variant_suffix}" if variant_suffix else base_name

    arg_lines: list[str] = []
    pragma_lines: list[str] = []
    for attr, ep in _discover_stream_endpoints(comp):
        tmpl_arg = _stream_template_arg(ep)
        arg_lines.append(
            f"    hls::stream<streamutils::axi4s_word<{tmpl_arg}>>& {attr}"
        )
        pragma_lines.append(f"#pragma HLS INTERFACE axis port={attr}")
    regmap_slave = _discover_regmap(comp)
    if regmap_slave is not None:
        for fname, fld in regmap_slave.regmap._fields.items():
            if fld.is_vitis_auto:
                continue
            schema = fld.schema
            if (isinstance(schema, type) and issubclass(schema, DataArray)
                    and getattr(schema, 'cpp_storage', 'struct') == 'raw'):
                elem_cpp = cpp_type(schema.element_type)
                count = schema._declared_count()
                arg_lines.append(f"    {elem_cpp} {fname}[{count}]")
            else:
                arg_lines.append(f"    {cpp_type(schema)}& {fname}")
            pragma_lines.append(
                f"#pragma HLS INTERFACE s_axilite port={fname:<12} bundle=control"
            )
    # m_axi master ports — appended after streams + regmap fields, in canonical
    # signature order (streams, regmap, m_axi).  Each maps to an ap_uint<bw>
    # pointer + an m_axi pragma (mirrors the generated histogram kernel).
    mm_masters = _discover_mm_masters(comp)
    for attr, ep in mm_masters:
        bw = int(ep.bitwidth)
        arg_lines.append(f"    ap_uint<{bw}>* {attr}")
        # depth is the total region the master sees — a named header constant
        # summing this port's buffer bounds (see header_to_cpp).
        # max_*_burst_length=256 (the AXI4 max) lets bulk transfers coalesce into
        # full bursts; without it Vitis caps bursts at the default 16 and
        # partitions every read_array_slice into 16x more requests (measured ~8x
        # slower under the cosim's per-burst stall model).  It is a *cap*, not a
        # forced size — small transfers still burst small; the only cost is the
        # adapter's burst-buffer BRAM.  TODO: make per-port configurable (a small
        # random-access port wants a smaller cap).
        pragma_lines.append(
            f"#pragma HLS INTERFACE m_axi port={attr} offset=slave "
            f"bundle=gmem depth={attr}_depth "
            f"max_read_burst_length=256 max_write_burst_length=256"
        )
    # Control protocol on the return port: s_axilite when a regmap drives
    # control (poly), else ap_ctrl_hs for stream-controlled kernels (histogram /
    # this toy — decision 2/3).
    if regmap_slave is not None:
        pragma_lines.append(
            "#pragma HLS INTERFACE s_axilite port=return       bundle=control"
        )
    elif mm_masters:
        pragma_lines.append("#pragma HLS INTERFACE ap_ctrl_hs port=return")
    else:
        pragma_lines.append(
            "#pragma HLS INTERFACE s_axilite port=return       bundle=control"
        )
    arg_block = ",\n".join(arg_lines)
    pragma_block = "\n".join(pragma_lines)
    return f"void {name}(\n{arg_block}\n) {{\n{pragma_block}"


def hook_signature(
    method, template_params: list[str] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(return_cpp_type, [(arg_name, arg_decl), ...])`` for a hook.

    Drops ``self``. Unwraps ``ProcessGen[T]`` return annotations (which alias
    to ``Generator[Event, Any, T]``). Stream-endpoint args become axis
    ``hls::stream<...>&`` references; other args translate via :func:`cpp_type`.

    When ``template_params`` is non-empty, the i-th stream-typed arg uses
    the i-th template-param name in its ``axi4s_word<...>`` expression
    instead of the symbolic ``WORD_BW``.
    """
    import collections.abc
    import inspect
    import typing

    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    from waveflow.hw.memif import MMIFMaster

    hints = typing.get_type_hints(method)
    sig = inspect.signature(method)
    param_names = [name for name in sig.parameters if name != 'self']
    tparam_queue = list(template_params) if template_params else []
    args: list[tuple[str, str]] = []
    # An ``MMIFMaster`` hook arg lowers to the raw-word m_axi pointer ``ap_uint<bw>*``
    # (the §2 serialization convention).  Its width is the concrete bitwidth of the
    # component's m_axi master (resolved from the hook's bound ``self`` — like the top
    # signature does), so the pointer type is concrete even though the hook is templated
    # on its stream widths.
    _self = getattr(method, '__self__', None)
    _mm_bw = None
    if _self is not None:
        _mm = _discover_mm_masters(_self)
        if len(_mm) == 1:
            _mm_bw = int(_mm[0][1].bitwidth)
    # ``template_params`` is deduped (one entry per distinct HwParam width), but a hook can have
    # several stream args sharing one width (e.g. a free-running kernel whose s_in and m_out are
    # both ``mem_dwidth``).  Consume sequentially, but when the queue exhausts fall back to the
    # last consumed param (the shared width) rather than the symbolic ``WORD_BW`` (undefined).
    _last_tmpl: str | None = None
    for name in param_names:
        annot = hints.get(name)
        if annot is None:
            raise RuntimeError(
                f"Hook '{method.__name__}' parameter '{name}' has no type annotation"
            )
        if isinstance(annot, type) and issubclass(annot, (StreamIFSlave, StreamIFMaster)):
            if tparam_queue:
                _last_tmpl = tparam_queue.pop(0)
            tmpl = _last_tmpl if _last_tmpl is not None else "WORD_BW"
            args.append(
                (name, f"hls::stream<streamutils::axi4s_word<{tmpl}>>& {name}")
            )
            continue
        if isinstance(annot, type) and issubclass(annot, MMIFMaster):
            if _mm_bw is None:
                raise RuntimeError(
                    f"Hook '{method.__name__}' arg '{name}' is an MMIFMaster but the "
                    f"component has no single resolvable m_axi master to size the pointer"
                )
            args.append((name, f"ap_uint<{_mm_bw}>* {name}"))
            continue
        if (isinstance(annot, type) and issubclass(annot, DataArray)
                and getattr(annot, 'cpp_storage', 'struct') == 'raw'):
            elem_cpp = cpp_type(annot.element_type)
            count = annot._declared_count()
            args.append((name, f"{elem_cpp} {name}[{count}]"))
            continue
        args.append((name, f"{cpp_type(annot)} {name}"))
    ret = hints.get('return')
    if ret is None:
        ret_cpp = "void"
    else:
        # ProcessGen[T] = Generator[Event, Any, T]; unwrap to T.
        if typing.get_origin(ret) is collections.abc.Generator:
            gen_args = typing.get_args(ret)
            if len(gen_args) == 3:
                ret = gen_args[2]
        if ret is type(None):
            ret_cpp = "void"
        elif (isinstance(ret, type) and issubclass(ret, DataArray)
                and getattr(ret, 'cpp_storage', 'struct') == 'raw'):
            # An array return can't be returned by value in HLS — lower it to a
            # void function with an appended out-parameter the kernel fills.
            elem_cpp = cpp_type(ret.element_type)
            args.append(("out", f"{elem_cpp} out[{ret._declared_count()}]"))
            ret_cpp = "void"
        else:
            ret_cpp = cpp_type(ret)
    return ret_cpp, args


def hook_signature_str(
    method, template_params: list[str] | None = None,
) -> str:
    """Return the full C++ declaration string for a hook (no body, no semicolon).

    When ``template_params`` is non-empty, the result is prefixed with a
    ``template <int p1, int p2, ...>`` line.
    """
    ret_cpp, args = hook_signature(method, template_params=template_params)
    arg_str = ", ".join(arg_decl for _, arg_decl in args)
    prefix = ""
    if template_params:
        params = ", ".join(f"int {p}" for p in template_params)
        prefix = f"template <{params}>\n"
    return f"{prefix}{ret_cpp} {method.__name__}({arg_str})"


# ---------------------------------------------------------------------------
# Hook templating (Phase 10): detect HwParam-driven stream args at the
# call site and decide whether a hook is emitted as a template (in a .tpp
# include) or a plain free function (in a .cpp impl).
# ---------------------------------------------------------------------------


def hook_template_params(stmt: FunctionStmt) -> list[str]:
    """Return the ordered, deduplicated ``HwParam`` names this hook is templated on.

    Walks ``stmt.inputs`` (call-site arguments), collecting the ``param_name``
    of any stream endpoint whose ``bitwidth`` is a ``HwParamValue``. An empty
    list means the hook is NOT templated and should be emitted in a ``.cpp``
    file. A non-empty list means the hook is templated and goes in a ``.tpp``.
    """
    from waveflow.hw.hw_component import HwParamValue
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave

    params: list[str] = []
    seen: set[str] = set()
    for inp in stmt.inputs:
        if isinstance(inp, (StreamIFSlave, StreamIFMaster)):
            bw = inp.bitwidth
            if isinstance(bw, HwParamValue) and bw.param_name not in seen:
                params.append(bw.param_name)
                seen.add(bw.param_name)
    return params


def _stmt_children(node) -> list:
    """Return immediate child statements of ``node`` for recursive tree walks."""
    if isinstance(node, WhileStmt):
        return [node.body]
    if isinstance(node, SeqStmt):
        return list(node.stmts)
    if isinstance(node, CaseStmt):
        children = [node.if_true]
        if node.if_false is not None:
            children.append(node.if_false)
        return children
    return []


def _validate_single_call_site(tree, hook_method, template_params: list[str]) -> None:
    """Templated hooks must be called from one site (or consistently across sites).

    Walks the tree, collects ``hook_template_params`` for every
    ``FunctionStmt`` targeting ``hook_method``, and raises ``SynthesisError``
    if any site yields a different param list.
    """
    from waveflow.build.hwcodegen import SynthesisError

    sites: list[list[str]] = []

    def visit(node):
        if isinstance(node, FunctionStmt) and node.method is hook_method:
            sites.append(hook_template_params(node))
        for child in _stmt_children(node):
            visit(child)

    visit(tree)
    if len(sites) > 1 and any(s != sites[0] for s in sites[1:]):
        raise SynthesisError(
            f"Templated hook '{hook_method.__name__}' called from "
            f"{len(sites)} sites with inconsistent template params: {sites}"
        )


def _snake_case(name: str) -> str:
    """``CamelCase`` / ``PascalCase`` / ``mixedCase`` → ``snake_case``.

    Inserts an underscore before each upper-case letter (except a leading
    one), then lowercases the whole string. Digits stay attached to the
    preceding word: ``Float32`` → ``float32``.
    """
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def cpp_kernel_name(comp_class) -> str:
    """Default kernel function name.

    ``CamelCase`` → ``snake_case`` with a trailing ``_component`` stripped:
    ``DemoComponent → demo``, ``PolyAccelComponent → poly_accel``. Override
    per class by setting ``cpp_kernel_name: ClassVar[str | None] = "..."``.
    """
    override = getattr(comp_class, 'cpp_kernel_name', None)
    if override:
        return override
    name = comp_class.__name__
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    return snake.removesuffix('_component')


def resolved_namespace(comp_class) -> str | None:
    """Return the C++ namespace to use for this component's hooks.

    ``None`` means "no namespace, emit in global." Otherwise the returned
    string is the namespace name to wrap hooks in. Resolution rules:

    - ``cpp_namespace = ""`` → ``None`` (opt out; hooks emitted in global).
    - ``cpp_namespace = None`` → auto-derive from :func:`cpp_kernel_name`.
    - ``cpp_namespace = "<name>"`` → return ``"<name>"`` verbatim.
    """
    ns = getattr(comp_class, 'cpp_namespace', None)
    if ns == "":
        return None
    if ns is None:
        return cpp_kernel_name(comp_class)
    return ns


def _collect_schemas(tree: HwStmt, comp) -> list[type]:
    """Return the unique ``DataSchema`` subclasses referenced by the kernel.

    ``IntField`` and ``FloatField`` subclasses are excluded — they map to
    primitive C++ types (``ap_uint<N>`` / ``float``) and have no header of
    their own.  Matches the include set in ``poly.hpp``.

    Sources walked:
      - ``HwVar.typ`` on every ``stmt.outputs`` in the resolved tree.
      - Hook signatures: arg and return annotations of every ``FunctionStmt``
        method.  ``ProcessGen[T]`` returns unwrap to ``T``; stream-endpoint
        types are skipped.
      - Every regmap field schema (if the component carries a regmap).
    """
    import collections.abc
    import typing as _typing

    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave

    def _has_header(typ) -> bool:
        if not (isinstance(typ, type) and issubclass(typ, DataSchema)):
            return False
        if issubclass(typ, (IntField, FloatField)):
            return False
        # A typing-only buffer DataArray (used purely to type a hook's array
        # param/return as ``elem[n]``) has no schema header and contributes no
        # includes — its array-utils come from the m_axi op that fills it.
        if getattr(typ, 'cpp_typing_only', False):
            return False
        return True

    schemas: dict[str, type] = {}

    def _add(typ) -> None:
        if _has_header(typ):
            schemas[typ.cpp_class_name()] = typ

    def _add_hook_annotations(method) -> None:
        try:
            hints = _typing.get_type_hints(method)
        except Exception:
            return
        for name, hint in hints.items():
            if name == 'return':
                # ProcessGen[T] = Generator[Event, Any, T]; unwrap to T.
                if _typing.get_origin(hint) is collections.abc.Generator:
                    gen_args = _typing.get_args(hint)
                    if len(gen_args) == 3:
                        hint = gen_args[2]
            # Stream-endpoint arg types are not schemas; skip them.
            if (
                isinstance(hint, type)
                and issubclass(hint, (StreamIFSlave, StreamIFMaster))
            ):
                continue
            _add(hint)

    def visit(node):
        if isinstance(node, WhileStmt):
            visit(node.body)
        elif isinstance(node, SeqStmt):
            for c in node.stmts:
                visit(c)
        elif isinstance(node, CaseStmt):
            visit(node.if_true)
            if node.if_false is not None:
                visit(node.if_false)
        for v in getattr(node, 'outputs', []):
            _add(v.typ)
        if isinstance(node, FunctionStmt):
            _add_hook_annotations(node.method)

    visit(tree)
    regmap_slave = _discover_regmap(comp)
    if regmap_slave is not None:
        for fld in regmap_slave.regmap._fields.values():
            _add(fld.schema)
    return list(schemas.values())


def _collect_hooks(tree: HwStmt) -> list:
    """Return the bound methods of every ``FunctionStmt`` reachable from ``tree``."""
    result: list = []
    seen: set[int] = set()

    def visit(node):
        if isinstance(node, FunctionStmt):
            key = id(node.method)
            if key not in seen:
                seen.add(key)
                result.append(node.method)
        if isinstance(node, WhileStmt):
            visit(node.body)
        elif isinstance(node, SeqStmt):
            for c in node.stmts:
                visit(c)
        elif isinstance(node, CaseStmt):
            visit(node.if_true)
            if node.if_false is not None:
                visit(node.if_false)

    visit(tree)
    return result


def _collect_hooks_with_params(tree: HwStmt) -> list[tuple[object, list[str]]]:
    """Walk ``tree`` and return ``[(method, template_params)]`` per unique hook.

    Validates that every call site of a given hook produces the same
    ``template_params``; raises ``SynthesisError`` on mismatch.
    """
    seen: dict[int, tuple[object, list[str]]] = {}

    def visit(node):
        if isinstance(node, FunctionStmt):
            # Dedup by the underlying function, not the bound-method object —
            # each ``self.hook`` access is a fresh bound method (distinct id), so
            # a hook called at several sites would otherwise appear repeatedly.
            key = id(getattr(node.method, '__func__', node.method))
            tparams = hook_template_params(node)
            if key not in seen:
                seen[key] = (node.method, tparams)
            else:
                _, existing = seen[key]
                if existing != tparams:
                    from waveflow.build.hwcodegen import SynthesisError
                    raise SynthesisError(
                        f"Hook '{node.method.__name__}' called with inconsistent "
                        f"template params: {existing} vs {tparams}"
                    )
        for child in _stmt_children(node):
            visit(child)

    visit(tree)
    return list(seen.values())


def _collect_utility_includes(schemas: list[type]) -> list[str]:
    """Union of ``DataSchema.get_utility_includes()`` across ``schemas``.

    Recurses through each schema's ``get_dependencies()`` to pick up
    utility includes carried by transitive deps that didn't appear directly
    in the collected list.  Order-preserving dedup.
    """
    paths: list[str] = []
    seen: set[str] = set()
    visited: set[type] = set()

    def add(schema) -> None:
        if schema in visited:
            return
        visited.add(schema)
        for path in schema.get_utility_includes():
            if path not in seen:
                seen.add(path)
                paths.append(path)
        for dep in schema.get_dependencies():
            add(dep)

    for s in schemas:
        add(s)
    return paths


def _kernel_signature_decl(comp, variant_suffix: str = "") -> str:
    """Same as :func:`kernel_signature` but without pragmas; trailing ``;``."""
    full = kernel_signature(comp, variant_suffix=variant_suffix)
    head, _sep, _tail = full.partition('\n) {')
    return head + '\n);'


def _impl_include_path(filename: str, output_dir: str, impl_dir: str | None) -> str:
    """Relative include path from ``output_dir`` (where the .hpp lives) to the
    impl file (in ``impl_dir`` or ``output_dir`` when ``impl_dir`` is None).

    Always returns forward slashes for cross-platform HLS toolchain portability.
    """
    import os.path
    impl_root = impl_dir if impl_dir is not None else output_dir
    if impl_root == output_dir:
        return filename
    rel_dir = os.path.relpath(impl_root, start=output_dir)
    rel = os.path.join(rel_dir, filename) if rel_dir != "." else filename
    return rel.replace("\\", "/")


def _iter_variants(comp_class):
    """Yield ``(variant_suffix, comp_instance)`` for each variant to emit.

    Always yields the default first (suffix ``""``).  Then yields each entry
    from ``comp_class.param_supports`` (if any) with the variant overrides
    applied via the normal ``__init__`` path — no immutability bypass.
    """
    from waveflow.build.elaborate import elaborate
    from waveflow.hw.hw_component import validate_param_supports

    validate_param_supports(comp_class)
    yield "", elaborate(comp_class)
    ps = getattr(comp_class, 'param_supports', None) or {}
    for suffix, overrides in ps.items():
        yield suffix, elaborate(comp_class, overrides)


def header_to_cpp(
    comp_class,
    output_dir: str = ".",
    impl_dir: str | None = None,
) -> str:
    """Build the content of ``<component>.hpp``.

    Emits one concrete forward declaration per kernel variant (default
    ``<cpp_kernel_name>``, plus any entries from ``param_supports``).
    No ``template <int ...>`` block at the top level — kernels are
    concrete so Vitis HLS RTL generation can attach interfaces.

    Hooks remain templated; their forward decls are grouped inside a
    single ``namespace <ns> { ... }`` block when one is resolved.  The
    hook decls and ``#include`` lines for any ``.tpp`` files are derived
    from the *default* variant (hook signatures don't vary per variant).
    The path on each ``#include`` line is relative from ``output_dir`` to
    ``impl_dir``.
    """
    from waveflow.build.hwcodegen import extract_kernel

    variants = list(_iter_variants(comp_class))
    default_comp = variants[0][1]

    tree = extract_kernel(default_comp)
    schemas = _collect_schemas(tree, default_comp)
    # Always provide ap_int / ap_fixed: a kernel signature can use these via s_axilite
    # register fields even when the component has no streams / schemas / m_axi (which
    # otherwise pull in streamutils_hls.h, the header that supplies them). A regmap-only
    # scalar kernel (e.g. simp_fun) would otherwise emit a header that uses ap_int<W> with
    # nothing defining it. Redundant-include-safe via header guards.
    lines = ['#pragma once', '', '#include <ap_int.h>', '#include <ap_fixed.h>', '']
    if _discover_stream_endpoints(default_comp):
        lines.append('#include "include/streamutils_hls.h"')
    for s in schemas:
        lines.append(f'#include "include/{_snake_case(s.cpp_class_name())}.h"')  # type: ignore[attr-defined]
    for path in _collect_utility_includes(schemas):
        lines.append(f'#include "{path}"')
    # m_axi support: the memory manager (byte→word addressing) + the
    # array-utils header for each element type read/written over m_axi
    # (deduped against the utility includes already emitted above).
    mm_masters = _discover_mm_masters(default_comp)
    if mm_masters:
        from waveflow.hw.arrayutils import _array_utils_include_path
        for inc in ['#include "include/memmgr.hpp"'] + [
            f'#include "{_array_utils_include_path(et)}"'
            for et in _collect_mm_elem_types(tree)
        ]:
            if inc not in lines:
                lines.append(inc)
        # The reusable ring-poll primitive backing a synthesizable poll_until.
        if _tree_has_poll_until(tree):
            inc = '#include "poll_until_impl.tpp"'
            if inc not in lines:
                lines.append(inc)
        # Compile-time bounds: the HwParam buffer maxes read in the kernel, then
        # each m_axi port's depth = the sum of its buffer bounds.
        lines.append('')
        for pname, pval in _kernel_hwparam_constants(tree).items():
            lines.append(f'static const int {pname} = {pval};')
        depth_ctx = CodegenCtx(comp=default_comp)
        for attr, ep in mm_masters:
            lines.append(
                f'static const int {attr}_depth = {_mm_port_depth(ep, tree, depth_ctx)};'
            )
        # Total m_axi region + interface widths + word typedefs — not used by the
        # kernel (which inlines its widths), but make the header a drop-in for a
        # testbench that drives the kernel (it sizes the flat memory array and the
        # axis/mem word types from these).
        all_maxes = [
            _emit_expr(s.max_expr, depth_ctx)
            for s in _collect_mm_stmts(tree) if s.max_expr is not None
        ]
        if all_maxes:
            lines.append(f'static const int max_mem_words = {" + ".join(all_maxes)};')
        streams = _discover_stream_endpoints(default_comp)
        if streams:
            lines.append(f'static const int stream_dwidth = {int(streams[0][1].bitwidth)};')
        lines.append(f'static const int mem_dwidth = {int(mm_masters[0][1].bitwidth)};')
        mem_awidth = getattr(default_comp, 'mem_awidth', None)
        if mem_awidth is not None:
            lines.append(f'static const int mem_awidth = {int(mem_awidth)};')
        if streams:
            lines.append('using axis_word_t = streamutils::axi4s_word<stream_dwidth>;')
        lines.append('using mem_word_t = ap_uint<mem_dwidth>;')

    for suffix, variant_comp in variants:
        lines.append('')
        lines.append(_kernel_signature_decl(variant_comp, variant_suffix=suffix))

    hooks = _collect_hooks_with_params(tree)
    if hooks:
        ns = resolved_namespace(comp_class)
        lines.append('')
        indent = "    " if ns is not None else ""
        if ns is not None:
            lines.append(f"namespace {ns} {{")
        for hook, tparams in hooks:
            decl = hook_signature_str(hook, template_params=tparams)
            decl_lines = decl.split("\n")
            decl_lines[-1] += ";"
            for line in decl_lines:
                lines.append(f"{indent}{line}")
        if ns is not None:
            lines.append("}")

    templated = [(h, p) for h, p in hooks if p]
    if templated:
        lines.append("")
        kn = cpp_kernel_name(comp_class)
        for hook, _ in templated:
            hname = hook.__name__  # type: ignore[attr-defined]
            include_path = _impl_include_path(
                f"{kn}_{hname}_impl.tpp", output_dir, impl_dir,
            )
            lines.append(f'#include "{include_path}"')

    return "\n".join(lines) + "\n"


def kernel_body_to_cpp(comp: HwComponent) -> str:
    """Top-level driver: extract + resolve + codegen.

    Returns the body of the kernel function (opened with ``{``, closed with
    ``}``).  No function signature, no pragmas — just the body.  Wrap
    separately in the next phase.
    """
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.hw.hw_component import SynthContext

    tree = extract_kernel(comp)
    synth_ctx = SynthContext.from_component(comp)
    ctx = CodegenCtx(comp=comp, params=synth_ctx.params, indent=1)
    body = to_cpp(tree, ctx)
    return f"{{\n{body}\n}}"


def kernel_to_cpp(comp_class) -> str:
    """Build the content of ``<component>.cpp``.

    Emits one concrete kernel function per variant — the default uses
    ``HwParam`` defaults; any entries in ``param_supports`` add named
    variants ``<cpp_kernel_name>_<key>``.  No template instantiation
    machinery: each variant is a fully concretized top-level function so
    Vitis HLS can attach AXI interfaces during RTL generation.

    Takes a component **class**, not an instance — the function creates
    a fresh instance per variant internally.
    """
    header_name = f"{cpp_kernel_name(comp_class)}.hpp"
    parts: list[str] = [f'#include "{header_name}"', ""]
    # byte_addr_to_word_index lives in waveflow::memmgr; alias it like
    # the generated histogram kernel when the kernel has m_axi masters.
    default_comp = next(iter(_iter_variants(comp_class)))[1]
    if _discover_mm_masters(default_comp):
        parts.append("namespace memmgr = waveflow::memmgr;")
        parts.append("")
    for variant_suffix, variant_comp in _iter_variants(comp_class):
        sig_with_pragmas = kernel_signature(
            variant_comp, variant_suffix=variant_suffix,
        )
        body = kernel_body_to_cpp(variant_comp)
        body_inner = body.removeprefix("{\n").removesuffix("\n}")
        parts.append(f"{sig_with_pragmas}\n{body_inner}\n}}")
        parts.append("")
    return "\n".join(parts) + "\n"


def _stub_default_return(ret_cpp: str) -> str:
    """Best-effort default ``return ...;`` for a stub body so the file compiles."""
    if ret_cpp == "void":
        return ""
    if ret_cpp.startswith("ap_uint") or ret_cpp.startswith("ap_int"):
        return f"return {ret_cpp}(0);"
    if ret_cpp in ("float", "double"):
        return f"return {ret_cpp}(0);"
    # struct: default-construct
    return f"return {ret_cpp}{{}};"


def impl_stub_to_cpp(comp, hook_method) -> str:
    """Build the first-time stub content for one hook impl file.

    When a namespace is resolved for the component, the function
    definition is wrapped in a ``namespace <ns> { ... }`` block.
    """
    header_name = f"{cpp_kernel_name(type(comp))}.hpp"
    ret_cpp, args = hook_signature(hook_method)
    arg_str = ", ".join(arg_decl for _, arg_decl in args)
    default = _stub_default_return(ret_cpp)
    body_lines = [f"    // TODO: implement {hook_method.__name__}"]
    if default:
        body_lines.append(f"    {default}")
    body = "\n".join(body_lines)
    func_def = (
        f"{ret_cpp} {hook_method.__name__}({arg_str}) {{\n"
        f"{body}\n"
        f"}}"
    )
    ns = resolved_namespace(type(comp))
    if ns is not None:
        func_def = f"namespace {ns} {{\n{func_def}\n}}"
    return (
        f'#include "{header_name}"\n\n'
        f"{func_def}\n"
    )


def impl_stub_to_tpp(comp, hook_method, template_params: list[str]) -> str:
    """Build the first-time stub content for one templated hook's ``.tpp`` file.

    The ``.tpp`` is meant to be ``#include``'d from ``<component>.hpp`` at the
    bottom, so it does **not** include the header itself — types declared in
    the header are already in scope.  The function definition is wrapped in
    a ``namespace <ns> { ... }`` block when one is resolved.
    """
    ret_cpp, args = hook_signature(hook_method, template_params=template_params)
    arg_str = ", ".join(arg_decl for _, arg_decl in args)
    default = _stub_default_return(ret_cpp)
    body_lines = [f"    // TODO: implement {hook_method.__name__}"]
    if default:
        body_lines.append(f"    {default}")
    body = "\n".join(body_lines)
    tparam_str = ", ".join(f"int {p}" for p in template_params)
    func_def = (
        f"template <{tparam_str}>\n"
        f"{ret_cpp} {hook_method.__name__}({arg_str}) {{\n"
        f"{body}\n"
        f"}}"
    )
    ns = resolved_namespace(type(comp))
    if ns is not None:
        func_def = f"namespace {ns} {{\n{func_def}\n}}"
    kn = cpp_kernel_name(type(comp))
    header_comment = (
        f"// This file is included from {kn}.hpp at the bottom — types\n"
        "// declared there are in scope. Do not include this file directly\n"
        "// except via the .hpp.\n\n"
    )
    return header_comment + func_def + "\n"


def tb_files_to_str(
    tb_class,
    output_dir: str = ".",
) -> dict[str, str]:
    """Build the file set for a ``HwTestbench`` subclass.

    Returns ``{filename: contents}`` for the single ``<kernel>_tb.cpp``
    file emitted in testbench mode.  ``cpp_kernel_name`` controls the
    filename: testbench classes are expected to set it to match the DUT
    they test (e.g. ``cpp_kernel_name = "poly"`` on a ``PolyTBHls``
    yields ``gen/poly_tb.cpp``).
    """
    name = cpp_kernel_name(tb_class)
    return {f"{name}_tb.cpp": _testbench_cpp(tb_class)}


@dataclass
class TbCodegenCtx:
    """Codegen context for the testbench-mode emitter.

    Mirrors :class:`CodegenCtx` but with side tables for bound TB locals:

    - ``duts``: ``local_name -> instantiated HwComponent`` produced by
      :class:`DutBindStmt`.
    - ``schemas``: ``local_name -> DataSchema subclass`` produced by
      :class:`SchemaBindStmt`.
    - ``stream_bitwidth``: ``(dut_local, endpoint_attr) -> int`` cached
      bitwidth used for ``write_axi4_stream<BW>`` template args.

    The TB body is sequential, so a single shared context (no scoping)
    is sufficient.
    """
    comp: object   # the HwTestbench instance
    indent: int = 1
    duts: dict[str, object] = field(default_factory=dict)
    schemas: dict[str, type] = field(default_factory=dict)
    mems: dict[str, tuple] = field(default_factory=dict)  # local -> (word_size, nwords_tot)

    def pad(self) -> str:
        return "    " * self.indent

    def child(self) -> TbCodegenCtx:
        return TbCodegenCtx(
            comp=self.comp,
            indent=self.indent + 1,
            duts=self.duts,
            schemas=self.schemas,
            mems=self.mems,
        )


def tb_to_cpp(stmt: HwStmt, ctx: TbCodegenCtx) -> str:
    """Emit C++ source for a testbench-mode statement."""
    if isinstance(stmt, SeqStmt):
        return "\n".join(tb_to_cpp(c, ctx) for c in stmt.stmts)
    if isinstance(stmt, DutBindStmt):
        return _emit_dut_bind(stmt, ctx)
    if isinstance(stmt, MemBindStmt):
        return _emit_mem_bind(stmt, ctx)
    if isinstance(stmt, MemAllocArrayStmt):
        return _emit_mem_alloc_array(stmt, ctx)
    if isinstance(stmt, MemReadArrayStmt):
        return _emit_mem_read_array(stmt, ctx)
    if isinstance(stmt, KernelCallStmt):
        return _emit_kernel_call(stmt, ctx)
    if isinstance(stmt, SchemaBindStmt):
        return _emit_schema_bind(stmt, ctx)
    if isinstance(stmt, TbFileIOStmt):
        return _emit_tb_file_io(stmt, ctx)
    if isinstance(stmt, TbStreamIOStmt):
        return _emit_tb_stream_io(stmt, ctx)
    if isinstance(stmt, TbRegmapFileReadStmt):
        return _emit_tb_regmap_file_read(stmt, ctx)
    if isinstance(stmt, TbStatusJsonStmt):
        return _emit_tb_status_json(stmt, ctx)
    raise NotImplementedError(
        f"Testbench codegen for {type(stmt).__name__} not implemented yet"
    )


def _emit_dut_bind(stmt: DutBindStmt, ctx: TbCodegenCtx) -> str:
    """Emit stream + regmap-field local decls for the bound DUT."""
    from waveflow.build.elaborate import elaborate
    dut = elaborate(stmt.comp_class, stmt.kwargs, name=f"_{stmt.local_name}")
    ctx.duts[stmt.local_name] = dut
    pad = ctx.pad()
    lines: list[str] = []
    for attr, ep in _discover_stream_endpoints(dut):
        tmpl = _stream_template_arg(ep)
        lines.append(
            f"{pad}hls::stream<streamutils::axi4s_word<{tmpl}>> {attr};"
        )
    regmap_slave = _discover_regmap(dut)
    if regmap_slave is not None:
        for fname, fld in regmap_slave.regmap._fields.items():
            if fld.is_vitis_auto:
                continue
            schema = fld.schema
            if (isinstance(schema, type) and issubclass(schema, DataArray)
                    and getattr(schema, 'cpp_storage', 'struct') == 'raw'):
                elem_cpp = cpp_type(schema.element_type)
                count = schema._declared_count()
                lines.append(f"{pad}{elem_cpp} {fname}[{count}] = {{}};")
            else:
                lines.append(f"{pad}{cpp_type(schema)} {fname} = 0;")
    return "\n".join(lines)


def _emit_mem_bind(stmt: MemBindStmt, ctx: TbCodegenCtx) -> str:
    """Emit the flat backing array + MemMgr for a ``MemComponent`` local."""
    ctx.mems[stmt.local_name] = (stmt.word_size, stmt.nwords_tot)
    pad = ctx.pad()
    bw = stmt.word_size
    nm = stmt.local_name
    return (
        f"{pad}static ap_uint<{bw}> {nm}[{stmt.nwords_tot}] = {{}};\n"
        f"{pad}waveflow::memmgr::MemMgr<{bw}> {nm}_mgr({nm}, {stmt.nwords_tot});"
    )


def _emit_mem_alloc_array(stmt: MemAllocArrayStmt, ctx: TbCodegenCtx) -> str:
    """``cmd.addr = mem.alloc_array(buf, ElemT, count=n)`` →
    ``MemMgr::alloc`` (word index) → byte address → ``write_array_slice`` populate."""
    pad = ctx.pad()
    bw, _nwords = ctx.mems[stmt.mem_local]
    ns = _array_utils_ns(stmt.elem_type)
    count = _emit_int_expr(stmt.count, ctx)
    widx = f"_{stmt.target_local}_{stmt.target_field}_widx"
    nwords = f"_{stmt.target_local}_{stmt.target_field}_nwords"
    mem = stmt.mem_local
    # Clamp the allocation to >= 1 word.  A 0-word region is meaningless and
    # MemMgr::alloc rejects it, but a runtime count can legitimately be 0 — the
    # nbins==1 (zero-edge) buffer, or a validation-failure case (ndata<=0 data,
    # nbins<=0 counts).  This mirrors the SimPy HistController, which allocs
    # max(nedges, 1) so such cases still get a valid address; the kernel then
    # reads/writes the runtime count (0 = a no-op burst).
    return (
        f"{pad}const int {nwords} = {ns}::get_nwords<{bw}>({count});\n"
        f"{pad}const int {widx} = {mem}_mgr.alloc({nwords} > 0 ? {nwords} : 1);\n"
        f"{pad}{stmt.target_local}.{stmt.target_field} = {widx} * ({bw} / 8);\n"
        f"{pad}{ns}::write_array_slice<{bw}>({stmt.src_local}, {mem} + {widx}, 0, {count});"
    )


def _emit_mem_read_array(stmt: MemReadArrayStmt, ctx: TbCodegenCtx) -> str:
    """``out = mem.read_array(addr, ElemT, count=n)`` →
    a static output buffer + a ``read_array_slice`` burst from the byte address."""
    from waveflow.hw.dataschema import DataArray
    pad = ctx.pad()
    bw, nwords = ctx.mems[stmt.mem_local]
    ns = _array_utils_ns(stmt.elem_type)
    elem_cpp = cpp_type(stmt.elem_type)
    count = _emit_int_expr(stmt.count, ctx)
    addr = _emit_int_expr(stmt.addr, ctx)
    out = stmt.target_local
    mem = stmt.mem_local
    # Register the output buffer as a raw-array schema local so a following
    # write_uint32_file_array(out, ...) resolves it.
    ctx.schemas[out] = DataArray.specialize(
        element_type=stmt.elem_type, max_shape=(nwords,), static=True,
    )
    return (
        f"{pad}static {elem_cpp} {out}[{nwords}] = {{}};\n"
        f"{pad}{ns}::read_array_slice<{bw}>("
        f"{mem} + waveflow::memmgr::byte_addr_to_word_index<{bw}>({addr}), "
        f"0, {count}, {out});"
    )


def _emit_kernel_call(stmt: KernelCallStmt, ctx: TbCodegenCtx) -> str:
    """Emit ``<kernel_name>(args...);`` matching the DUT's kernel signature.

    Arg order is canonical: stream endpoints (in ``vars(comp)`` order),
    then non-vitis-auto regmap fields (in declaration order) — the same
    iteration :func:`kernel_signature` uses, so emitter and signature
    stay in lockstep.
    """
    dut = ctx.duts.get(stmt.local_name)
    if dut is None:
        raise RuntimeError(
            f"KernelCallStmt for unbound DUT '{stmt.local_name}' — extractor "
            f"and emitter side tables are out of sync."
        )
    kn = cpp_kernel_name(type(dut))
    args: list[str] = [attr for attr, _ in _discover_stream_endpoints(dut)]
    regmap_slave = _discover_regmap(dut)
    if regmap_slave is not None:
        for fname, fld in regmap_slave.regmap._fields.items():
            if fld.is_vitis_auto:
                continue
            args.append(fname)
    # m_axi pointer(s) last — canonical signature order is streams, regmap,
    # m_axi (matches kernel_signature).  The TB passes the MemBindStmt array.
    if stmt.mem_local is not None:
        args.append(stmt.mem_local)
    return f"{ctx.pad()}{kn}({', '.join(args)});"


def _emit_schema_bind(stmt: SchemaBindStmt, ctx: TbCodegenCtx) -> str:
    """Emit a TB-mode local declaration for a bound :class:`DataSchema`.

    For raw-storage :class:`DataArray` subclasses the local is a C
    array sized at the schema's declared ``max_shape[0]``; everything
    else is a struct-typed local.  Either way we record the binding so
    subsequent file-IO / stream-IO statements can look it up.
    """
    cls = stmt.schema_class
    ctx.schemas[stmt.local_name] = cls
    pad = ctx.pad()
    if (isinstance(cls, type) and issubclass(cls, DataArray)
            and getattr(cls, 'cpp_storage', 'struct') == 'raw'):
        elem_cpp = cpp_type(cls.element_type)
        count = cls._declared_count()
        return f"{pad}{elem_cpp} {stmt.local_name}[{count}] = {{}};"
    return f"{pad}{cls.cpp_class_name()} {stmt.local_name};"


def _emit_tb_file_io(stmt: TbFileIOStmt, ctx: TbCodegenCtx) -> str:
    """Lower a single :class:`TbFileIOStmt` to the right utility call."""
    pad = ctx.pad()
    target = stmt.target_local
    path_cpp = _emit_str_expr(stmt.path, ctx)
    if stmt.is_array:
        cls = _require_array_schema(ctx, target)
        ns = _array_utils_ns(cls.element_type)
        fn = 'write_uint32_file_array' if stmt.is_write else 'read_uint32_file_array'
        count_cpp = _emit_int_expr(stmt.count, ctx)
        return (
            f"{pad}{ns}::{fn}({target}, "
            f"({path_cpp}).c_str(), {count_cpp});"
        )
    fn = 'write_uint32_file' if stmt.is_write else 'read_uint32_file'
    return f"{pad}streamutils::{fn}({target}, ({path_cpp}).c_str());"


def _emit_tb_stream_io(stmt: TbStreamIOStmt, ctx: TbCodegenCtx) -> str:
    """Lower a push/pop/push_array/pop_array TB call to the right C++ form."""
    pad = ctx.pad()
    bw = _resolve_stream_bw(stmt.dut_local, stmt.endpoint_attr, ctx)
    stream_name = stmt.endpoint_attr
    value = stmt.value_local
    if stmt.is_array:
        cls = _require_array_schema(ctx, value)
        ns = _array_utils_ns(cls.element_type)
        count_cpp = _emit_int_expr(stmt.count, ctx)
        if stmt.is_pop:
            tl_var = f"_tlast_{value}"
            return (
                f"{pad}streamutils::tlast_status {tl_var} = "
                f"streamutils::tlast_status::no_tlast;\n"
                f"{pad}{ns}::read_axi4_stream<{bw}>("
                f"{stream_name}, {value}, {tl_var}, {count_cpp});"
            )
        return (
            f"{pad}{ns}::write_axi4_stream<{bw}>("
            f"{stream_name}, {value}, true, {count_cpp});"
        )
    if stmt.is_pop:
        tl_var = f"_tlast_{value}"
        return (
            f"{pad}streamutils::tlast_status {tl_var} = "
            f"streamutils::tlast_status::no_tlast;\n"
            f"{pad}{value}.read_axi4_stream<{bw}>({stream_name}, {tl_var});"
        )
    return f"{pad}{value}.write_axi4_stream<{bw}>({stream_name}, true);"


def _emit_tb_regmap_file_read(
    stmt: TbRegmapFileReadStmt, ctx: TbCodegenCtx,
) -> str:
    """Emit scalar or raw-array regmap preload helper calls in TB mode."""
    pad = ctx.pad()
    dut = ctx.duts.get(stmt.dut_local)
    if dut is None:
        raise RuntimeError(
            f"TbRegmapFileReadStmt references unbound DUT '{stmt.dut_local}'"
        )
    regmap_slave = _discover_regmap(dut)
    if regmap_slave is None or stmt.field_name not in regmap_slave.regmap._fields:
        raise RuntimeError(
            f"DUT '{stmt.dut_local}' has no regmap field '{stmt.field_name}'"
        )
    fld = regmap_slave.regmap._fields[stmt.field_name]
    schema = fld.schema
    path_cpp = _emit_str_expr(stmt.path, ctx)
    if stmt.count is None:
        if schema.nwords_per_inst(regmap_slave.regmap.bitwidth) != 1:
            raise RuntimeError(
                f"dut.regmap.read_uint32_file only supports single-word fields; "
                f"field '{stmt.field_name}' is not."
            )
        word_cpp = "uint32_t" if regmap_slave.regmap.bitwidth <= 32 else "uint64_t"
        field_cpp = cpp_type(schema)
        return (
            f"{pad}{{\n"
            f"{pad}    std::ifstream _ifs(({path_cpp}).c_str(), std::ios::binary);\n"
            f"{pad}    if (!_ifs) {{\n"
            f"{pad}        throw std::runtime_error("
            f"\"Failed to open regmap input file for reading.\");\n"
            f"{pad}    }}\n"
            f"{pad}    {word_cpp} _word = 0;\n"
            f"{pad}    _ifs.read(reinterpret_cast<char*>(&_word), sizeof(_word));\n"
            f"{pad}    if (!_ifs) {{\n"
            f"{pad}        throw std::runtime_error("
            f"\"Failed to read regmap input file.\");\n"
            f"{pad}    }}\n"
            f"{pad}    {stmt.field_name} = ({field_cpp})_word;\n"
            f"{pad}}}"
        )
    if not (isinstance(schema, type) and issubclass(schema, DataArray)
            and getattr(schema, 'cpp_storage', 'struct') == 'raw'):
        raise RuntimeError(
            f"dut.regmap.read_uint32_file_array only supports raw-storage "
            f"DataArray fields; field '{stmt.field_name}' is not."
        )
    ns = _array_utils_ns(schema.element_type)
    count_cpp = _emit_int_expr(stmt.count, ctx)
    return (
        f"{pad}{ns}::read_uint32_file_array("
        f"{stmt.field_name}, ({path_cpp}).c_str(), {count_cpp});"
    )


def _emit_tb_status_json(stmt: TbStatusJsonStmt, ctx: TbCodegenCtx) -> str:
    """``dut.regmap.write_status_json(path, fields=[...])`` →
    inline ``std::ofstream`` block matching the hand-written
    ``examples/stream_inband/poly_tb.cpp`` schema (each field cast to ``int``).
    """
    pad = ctx.pad()
    path_cpp = _emit_str_expr(stmt.path, ctx)
    if not stmt.field_names:
        raise RuntimeError(
            "write_status_json requires a non-empty fields list"
        )
    lines = [
        f"{pad}{{",
        f"{pad}    std::ofstream _status_ofs(({path_cpp}));",
        f"{pad}    if (!_status_ofs) {{",
        f"{pad}        throw std::runtime_error("
        f'"Failed to open status JSON file for writing.");',
        f"{pad}    }}",
        f'{pad}    _status_ofs << "{{\\n";',
    ]
    last = len(stmt.field_names) - 1
    for i, fname in enumerate(stmt.field_names):
        comma = "" if i == last else ","
        lines.append(
            f'{pad}    _status_ofs << "  \\"{fname}\\": " '
            f'<< (int){fname} << "{comma}\\n";'
        )
    lines.append(f'{pad}    _status_ofs << "}}\\n";')
    lines.append(f"{pad}}}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TB-mode expression helpers
# ---------------------------------------------------------------------------


def _emit_str_expr(node, ctx: TbCodegenCtx) -> str:
    """Emit a TB-mode string expression (path argument) as C++.

    Handles ``ast.Constant(str)``, ``ast.Attribute(self, 'data_dir')``,
    ``ast.Name`` (raw locals), and ``ast.BinOp(Add)`` for concatenation.
    """
    import ast as _ast
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        # Embed as a C++ string literal.  We deliberately use std::string
        # so callers can ``.c_str()`` uniformly.
        escaped = (
            node.value
            .replace('\\', '\\\\')
            .replace('"', '\\"')
        )
        return f'std::string("{escaped}")'
    if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) \
            and node.value.id == 'self':
        # ``self.<attr>`` in TB main() resolves to a framework-provided
        # C++ local of the same name (``data_dir`` is the only one for
        # now, but the convention extends).
        return node.attr
    if isinstance(node, _ast.Name):
        return node.id
    if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Add):
        left = _emit_str_expr(node.left, ctx)
        right = _emit_str_expr(node.right, ctx)
        return f"{left} + {right}"
    if isinstance(node, _ast.Call):
        # Allow `<expr>.c_str()` if the user wrote it explicitly — strip
        # it since the emitter adds .c_str() at the I/O site.
        if (isinstance(node.func, _ast.Attribute)
                and node.func.attr == 'c_str' and not node.args):
            return _emit_str_expr(node.func.value, ctx)
    raise RuntimeError(
        f"Cannot lower TB string expression: {_ast.dump(node)}"
    )


def _emit_int_expr(node, ctx: TbCodegenCtx) -> str:
    """Emit a TB-mode integer/count expression as C++.

    Delegates to the kernel-side expression lowerer (:func:`_emit_ast_expr`) so a
    count such as ``nbins - 1`` lowers **identically** in the kernel and the
    testbench — a single source of truth that keeps them from diverging. Handles
    literals, names, ``local.field`` chains, and ``+``/``-``/``*`` arithmetic.
    """
    return _emit_ast_expr(node, ctx)


def _array_utils_ns(elem_type) -> str:
    """Return the ``<elem>_array_utils`` C++ namespace for ``elem_type``."""
    from waveflow.hw.arrayutils import _array_utils_namespace
    return _array_utils_namespace(elem_type)


def _require_array_schema(ctx: TbCodegenCtx, local: str) -> type:
    """Return the bound DataArray class for ``local``, raising if mismatched."""
    cls = ctx.schemas.get(local)
    if cls is None:
        raise RuntimeError(
            f"TB array op references unbound local '{local}' "
            f"— missing SchemaBindStmt"
        )
    if not (isinstance(cls, type) and issubclass(cls, DataArray)):
        raise RuntimeError(
            f"TB array op expected DataArray local '{local}', got {cls!r}"
        )
    return cls


def _resolve_stream_bw(
    dut_local: str, endpoint_attr: str, ctx: TbCodegenCtx,
) -> int:
    """Look up the integer bitwidth of a DUT stream endpoint."""
    dut = ctx.duts.get(dut_local)
    if dut is None:
        raise RuntimeError(
            f"Stream IO references unbound DUT '{dut_local}'"
        )
    ep = getattr(dut, endpoint_attr, None)
    if ep is None:
        raise RuntimeError(
            f"DUT '{dut_local}' has no endpoint '{endpoint_attr}'"
        )
    return int(ep.bitwidth)


# ---------------------------------------------------------------------------
# Testbench-mode include discovery + top-level driver
# ---------------------------------------------------------------------------


def _collect_tb_array_elem_types(tree: HwStmt, ctx: TbCodegenCtx) -> list[type]:
    """Walk the TB IR and return the unique array-element types referenced.

    Order-preserving dedup keyed on the element type's class identity.
    Drives the ``#include "include/<elem>_array_utils_tb.h"`` set in the
    generated testbench prologue.
    """
    seen: dict[int, type] = {}

    def add_array_cls(cls: type) -> None:
        if (isinstance(cls, type) and issubclass(cls, DataArray)
                and getattr(cls, 'cpp_storage', 'struct') == 'raw'):
            et = cls.element_type
            if id(et) not in seen:
                seen[id(et)] = et

    # SchemaBindStmt locals (TB main() body) + m_axi alloc/read element types.
    def visit(node):
        if isinstance(node, SeqStmt):
            for c in node.stmts:
                visit(c)
        elif isinstance(node, SchemaBindStmt):
            add_array_cls(node.schema_class)
        elif isinstance(node, (MemAllocArrayStmt, MemReadArrayStmt)):
            et = node.elem_type
            if id(et) not in seen:
                seen[id(et)] = et

    visit(tree)

    # Regmap fields of every bound DUT (file-read / kernel-call args)
    for dut in ctx.duts.values():
        regmap_slave = _discover_regmap(dut)
        if regmap_slave is None:
            continue
        for fld in regmap_slave.regmap._fields.values():
            if fld.is_vitis_auto:
                continue
            add_array_cls(fld.schema)

    return list(seen.values())


def _collect_tb_schemas(tree: HwStmt, ctx: TbCodegenCtx) -> list[type]:
    """Walk the TB IR and return the unique structured-schema classes used.

    Excludes raw-array schemas (those flow through ``_array_utils_tb.h``
    headers, not their own header).  ``IntField`` / ``FloatField`` map to
    primitive C++ types and have no per-schema header.
    """
    seen: dict[str, type] = {}

    def visit(node):
        if isinstance(node, SeqStmt):
            for c in node.stmts:
                visit(c)
        elif isinstance(node, SchemaBindStmt):
            cls = node.schema_class
            if not (isinstance(cls, type) and issubclass(cls, DataSchema)):
                return
            if issubclass(cls, (IntField, FloatField)):
                return
            if issubclass(cls, DataArray) \
                    and getattr(cls, 'cpp_storage', 'struct') == 'raw':
                return
            seen[cls.cpp_class_name()] = cls

    visit(tree)
    return list(seen.values())


def _testbench_cpp(tb_class) -> str:
    """Build the full ``<kernel>_tb.cpp`` content for a testbench class.

    Wraps the IR-driven body in the standard ``int main()`` boilerplate
    plus a discovered include block (kernel header, ``streamutils_tb.h``,
    one ``<elem>_array_utils_tb.h`` per array element type used, and
    explicit ``include/<schema>.h`` lines for structured schemas).
    """
    from waveflow.build.elaborate import elaborate
    from waveflow.build.hwcodegen import extract_testbench

    kn = cpp_kernel_name(tb_class)
    tb = elaborate(tb_class)
    tree = extract_testbench(tb)
    ctx = TbCodegenCtx(comp=tb, indent=1)
    body = tb_to_cpp(tree, ctx)

    elem_types = _collect_tb_array_elem_types(tree, ctx)
    schemas = _collect_tb_schemas(tree, ctx)

    include_lines: list[str] = [
        f'#include "{kn}.hpp"',
        '#include "include/streamutils_tb.h"',
    ]
    # m_axi support headers: MemMgr (alloc) + byte_addr_to_word_index, when the
    # TB declares a MemComponent backing array.
    if _tb_has_mem(tree):
        include_lines.append('#include "include/memmgr_tb.hpp"')
        include_lines.append('#include "include/memmgr.hpp"')
    for et in elem_types:
        stem = _array_utils_stem(et)
        include_lines.append(f'#include "include/{stem}_array_utils_tb.h"')
    for s in schemas:
        include_lines.append(
            f'#include "include/{_snake_case(s.cpp_class_name())}.h"'
        )

    lines = list(include_lines) + [
        '#include <fstream>',
        '#include <cstdint>',
        '#include <string>',
        '#include <stdexcept>',
        '',
        'int main(int argc, char** argv) {',
        '    const std::string data_dir = (argc > 1) ? argv[1] : "data";',
        '    (void)data_dir;',
    ]
    if body:
        lines.append(body)
    lines.append('    return 0;')
    lines.append('}')
    return "\n".join(lines) + "\n"


def _tb_has_mem(tree: HwStmt) -> bool:
    """True if the TB tree declares any ``MemComponent`` backing array."""
    found = False

    def visit(node):
        nonlocal found
        if isinstance(node, SeqStmt):
            for c in node.stmts:
                visit(c)
        elif isinstance(node, MemBindStmt):
            found = True

    visit(tree)
    return found


def _array_utils_stem(elem_type) -> str:
    """Bridge to :func:`waveflow.hw.arrayutils._array_utils_stem`."""
    from waveflow.hw.arrayutils import _array_utils_stem as _stem
    return _stem(elem_type)


def kernel_files_to_str(
    comp_class,
    output_dir: str = ".",
    impl_dir: str | None = None,
) -> dict[str, str]:
    """Top-level driver. Returns ``{filename: contents}`` for all generated files.

    Per-hook routing: a hook with a non-empty ``hook_template_params`` list
    is emitted as a templated ``.tpp`` stub; otherwise a plain ``.cpp`` stub.

    ``output_dir`` and ``impl_dir`` only influence the ``#include`` line
    that ``<component>.hpp`` emits for each templated impl file — the
    returned dict keys are still bare filenames.  Callers (e.g.
    ``HlsCodegenStep``) decide which directory each filename lands in.

    Takes a component **class** rather than an instance.  The class is
    used to drive the variant iteration (default + ``param_supports``
    entries); each variant gets its own instance internally.  Hook
    discovery uses the default variant since hook signatures don't vary
    per variant.
    """
    from waveflow.build.hwcodegen import extract_kernel

    name = cpp_kernel_name(comp_class)
    files: dict[str, str] = {
        f"{name}.hpp": header_to_cpp(comp_class, output_dir=output_dir, impl_dir=impl_dir),
        f"{name}.cpp": kernel_to_cpp(comp_class),
    }
    default_comp = next(iter(_iter_variants(comp_class)))[1]
    tree = extract_kernel(default_comp)
    for hook, tparams in _collect_hooks_with_params(tree):
        hname = hook.__name__  # type: ignore[attr-defined]
        if tparams:
            files[f"{name}_{hname}_impl.tpp"] = impl_stub_to_tpp(
                default_comp, hook, tparams,
            )
        else:
            files[f"{name}_{hname}_impl.cpp"] = impl_stub_to_cpp(default_comp, hook)
    return files
