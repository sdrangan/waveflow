"""HwStmtExtractor — build-time AST → HwStmt IR converter.

``HwStmtExtractor`` is an ``ast.NodeVisitor`` that parses the ``run_proc``
source of an ``HwModule``, recognises the synthesizable subset, and
returns a rooted ``HwStmt`` tree.  Everything outside the subset raises
``SynthesisError`` at build time.
"""
from __future__ import annotations

import ast
import inspect
import logging
import textwrap

_log = logging.getLogger(__name__)

from waveflow.hw.hwstmt import (
    CaseStmt,
    ContinueStmt,
    DutBindStmt,
    FieldRef,
    FunctionStmt,
    HwStmt,
    HwVar,
    KernelCallStmt,
    Ref,
    ReturnStmt,
    SchemaBindStmt,
    SeqStmt,
    SynthCallStmt,
    TbFileIOStmt,
    TbRegmapFileReadStmt,
    TbStatusJsonStmt,
    TbStreamIOStmt,
    WhileStmt,
)


_TB_FILE_IO_METHODS = frozenset({
    'read_uint32_file', 'write_uint32_file',
    'read_uint32_file_array', 'write_uint32_file_array',
})
_TB_STREAM_PUSH_METHODS = frozenset({'push', 'push_array'})
_TB_STREAM_POP_METHODS = frozenset({'pop', 'pop_array'})


class SynthesisError(Exception):
    """Raised when ``run_proc`` contains a non-synthesizable pattern."""


_PIPELINED_OP_NAMES = frozenset({'get_pipelined', 'write_pipelined'})

#: The SimPy spawn method.  ``<env>.process(gen)`` schedules *gen* as a **new** process that runs
#: alongside its caller — the one construct that makes a body concurrent by definition.
_SPAWN_METHOD_NAME = 'process'


def _is_env_ref(node: ast.expr) -> bool:
    """Is *node* an expression whose last name segment is ``env``?

    The receiver half of the spawn shape (see :meth:`HwStmtExtractor._validate_no_concurrency`).
    True for ``env``, ``self.env``, ``sim.env``, ``self.sim.env`` — every spelling of "the SimPy
    environment" that occurs in this codebase — and false for anything else.

    Deliberately narrow.  It matches on the *name* ``env`` rather than trying to resolve the
    receiver to a real ``simpy.Environment``, because the rule must hold syntactically: an
    extracted body is parsed, not executed, and a ``SeqTB`` has no ``env`` attribute to resolve
    against in the first place.  The cost of the narrowness is that an aliased environment
    (``e = self.env; e.process(...)``) is not seen; the benefit is that ``self.process(...)`` —
    which :mod:`waveflow.hw.interface` and :mod:`waveflow.simulation.simobj` use heavily as a
    library-internal spawn wrapper — cannot be mistaken for a user body's fan-out.
    """
    if isinstance(node, ast.Name):
        return node.id == 'env'
    if isinstance(node, ast.Attribute):
        return node.attr == 'env'
    return False


class HwStmtExtractor:
    """Parse ``run_proc`` of an ``HwModule`` into an ``HwStmt`` tree.

    Usage::

        extractor = HwStmtExtractor(comp)
        tree = extractor.extract()

    The returned tree is the root ``HwStmt`` (typically a ``WhileStmt`` for
    free-running components or a ``SeqStmt`` for per-invocation ones).
    """

    def __init__(
        self,
        comp,
        method_name: str = 'run_proc',
        is_testbench: bool = False,
    ) -> None:
        self._comp = comp
        self._method_name = method_name
        self._is_testbench = is_testbench
        self._scope: dict[str, HwVar] = {}
        # Globals of the extracted method — used to resolve bare-name call-site
        # references (e.g. the ``Eq`` / ``Ne`` poll-condition constructors).
        self._globals: dict = {}
        # Testbench-mode side tables — empty in kernel mode, populated by
        # the extractor as it walks the body in Phase 3/4.
        self._duts: dict[str, object] = {}      # local_name -> HwModule instance
        self._tb_locals: dict[str, object] = {} # local_name -> object (binding)
        self._mems: dict[str, object] = {}      # local_name -> MemModel class
        # TB locals that alias a bound DUT's *input* regmap field local (Stage 2b):
        # local_name -> (dut_local, field_name).  Populated by
        # ``x = <Schema>().read_uint32_file(path)``; consumed by ``run_once``/
        # ``run_once_sim`` arg validation so ``run_once_sim(x, a, b)`` passes the
        # values straight in.  See :meth:`_try_schema_file_read`.
        self._tb_field_locals: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def extract(self) -> HwStmt:
        method = getattr(self._comp, self._method_name)
        self._globals = getattr(method, '__globals__', {}) or {}
        src = inspect.getsource(method)
        src = textwrap.dedent(src)
        tree = ast.parse(src)
        func_def = tree.body[0]
        if not isinstance(func_def, ast.FunctionDef):
            raise SynthesisError(
                f"{self._method_name} source did not parse as a function"
            )
        # The sequential gate applies in BOTH modes: every target this extractor feeds is
        # sequential, so a spawn is disqualifying for a kernel body exactly as for a TB main().
        # It runs FIRST because it is the most fundamental verdict available — a concurrent body
        # is the wrong *flow*, and saying so beats reporting whatever rule the fan-out's arguments
        # happen to trip on the way past.
        self._validate_no_concurrency(func_def)
        if not self._is_testbench:
            # The implicit-capture rule applies to @synthesizable hook bodies
            # and on_start/run_proc.  Testbench main() bodies legitimately
            # read attributes of local DUTs (``dut.s_in.push(...)``), local
            # schema instances (``cmd_hdr.write_uint32_file(...)``), etc.,
            # so the rule isn't meaningful in TB mode — structural pattern
            # matching in _visit_stmt_tb handles validation instead.
            self._validate_no_implicit_capture(func_def)
        stmts = self._visit_stmts(func_def.body)
        if len(stmts) == 1:
            return stmts[0]
        return SeqStmt(stmts=stmts)

    # ------------------------------------------------------------------
    # Implicit-capture pre-pass
    # ------------------------------------------------------------------

    def _validate_no_implicit_capture(self, func_def: ast.FunctionDef) -> None:
        """Reject ``self.X`` attribute reads that aren't part of a known pattern.

        Allowed reads:
          - The function chain of a call whose target is ``@synthesizable`` or
            ``@sim_only`` (e.g. ``self.s_in.get`` in ``self.s_in.get(...)``).
          - Reads that resolve to a ``@sim_only`` callable.
          - Reads that resolve to an ``InterfaceEndpoint``, a ``RegMap``, or an
            ``AXIMMQueue`` (resource references passed into synthesizable calls).
          - Reads that resolve to a ``DataSchema`` subclass — a compile-time schema
            *type* passed as a codegen argument (e.g. ``self.Cmd`` in
            ``self.cmd_queue.get(self.Cmd)``, the instance-specialized analogue of
            hist's bare-global ``HistCmd``).  A schema class is a type, not a runtime
            field read, so it carries no hardware state to capture.

        Anything else — a plain field like ``self.proc_latency`` used in an
        expression — raises ``SynthesisError``.

        Subtrees of ``@sim_only`` call statements are skipped entirely so the
        rule doesn't apply to their arguments either.
        """
        from waveflow.hw.aximm_queue import AXIMMQueue
        from waveflow.hw.dataschema import DataSchema
        from waveflow.hw.hw_module import HwParamValue
        from waveflow.hw.interface import InterfaceEndpoint
        from waveflow.hw.regmap import RegMap

        extractor = self

        class _Validator(ast.NodeVisitor):
            def __init__(self) -> None:
                self._allowed_attr_ids: set[int] = set()

            def visit_Call(self, node: ast.Call) -> None:
                method = extractor._resolve_method(node.func)
                if getattr(method, '_is_sim_only', False):
                    return  # drop entire @sim_only call subtree
                self._mark_allowed(node.func)
                for arg in node.args:
                    self.visit(arg)
                for kw in node.keywords:
                    self.visit(kw.value)

            def _mark_allowed(self, attr_node: ast.expr) -> None:
                while isinstance(attr_node, ast.Attribute):
                    self._allowed_attr_ids.add(id(attr_node))
                    attr_node = attr_node.value

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if id(node) in self._allowed_attr_ids:
                    return
                root: ast.expr = node
                while isinstance(root, ast.Attribute):
                    root = root.value
                if not (isinstance(root, ast.Name) and root.id == 'self'):
                    self.generic_visit(node)
                    return
                obj = extractor._resolve_obj(node)
                if obj is not None and (
                    getattr(obj, '_is_sim_only', False)
                    or getattr(obj, '_is_synthesizable', False)
                    or isinstance(obj, (InterfaceEndpoint, RegMap, AXIMMQueue,
                                        HwParamValue))
                    or (isinstance(obj, type) and issubclass(obj, DataSchema))
                ):
                    return
                lineno = getattr(node, 'lineno', '?')
                raise SynthesisError(
                    f"Implicit capture of 'self.{node.attr}' at line {lineno}. "
                    f"Reads of self.X inside a synthesizable method are forbidden "
                    f"unless 'X' is @sim_only, an endpoint, or a RegMap. Mark the "
                    f"value @sim_only or pass it explicitly."
                )

        _Validator().visit(func_def)

    # ------------------------------------------------------------------
    # Sequential-gate pre-pass
    # ------------------------------------------------------------------

    def _validate_no_concurrency(self, func_def: ast.FunctionDef) -> None:
        """Reject a body that **spawns** a SimPy process — ``<env>.process(...)``.

        Every target this extractor feeds is *sequential*: a Vitis ``int main()`` or a
        straight-line kernel body.  A spawn forks a coroutine that runs alongside the rest of the
        body, so such a body has no straight-line lowering at all — it is a **structural**
        mismatch with the target, not a missing feature.  Without this rule the fan-out fails
        downstream at :meth:`_require_synthesizable` with *"Call to non-synthesizable method
        'process'"* — true, but it sends the author off to add a ``@synthesizable`` marker to
        SimPy, when the real answer is a different flow.

        **The detected shape**, and only it: an attribute call ``.process(...)`` whose receiver is
        an ``env``-ish expression (see :func:`_is_env_ref`) — ``env.process(...)``,
        ``self.env.process(...)``, ``sim.env.process(...)``, ``self.sim.env.process(...)``.

        **This is a gate, not a proof — do not read it as one.**  It rejects a syntactic construct
        that *certainly* implies concurrency; it does **not** certify that a body which passes is
        sequential.  Semantic interleaving (a testbench that interleaves writes and reads with the
        DUT running in between) is undecidable in general, and nothing here attempts it.  Passing
        this pre-pass means "no spawn was written", not "this body is sequential".
        """
        method_name = self._method_name
        for node in ast.walk(func_def):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr == _SPAWN_METHOD_NAME
                    and _is_env_ref(func.value)):
                continue
            lineno = getattr(node, 'lineno', '?')
            raise SynthesisError(
                f"Concurrent process spawn '{ast.unparse(func)}(...)' at line {lineno} of "
                f"{method_name}(). This body is concurrent: it spawns a coroutine that runs "
                f"alongside the rest of the body, so it has no straight-line lowering — neither "
                f"a sequential Vitis 'int main()' testbench nor a straight-line kernel body can "
                f"express the fork. It needs the SystemC path (Flow 3, free-running and "
                f"concurrently driven), not C-simulation; see docs/guide/flows/. If the body is "
                f"in fact sequential, run the coroutine inline ('yield from <call>') rather than "
                f"spawning it."
            )

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    def _visit_stmts(self, stmts: list[ast.stmt]) -> list[HwStmt]:
        return [s for s in (self._visit_stmt(n) for n in stmts) if s is not None]

    def _visit_stmt(self, stmt: ast.stmt) -> HwStmt | None:
        if self._is_testbench:
            tb_result = self._visit_stmt_tb(stmt)
            if tb_result is not None:
                return tb_result
            # Fall through to the kernel-mode handlers for statements that
            # apply identically in both modes (Return, If, Continue,
            # bare-Constant docstrings, etc.).
        if isinstance(stmt, ast.While):
            return self._visit_while(stmt)
        if isinstance(stmt, ast.Continue):
            return ContinueStmt()
        if isinstance(stmt, ast.Assign):
            return self._visit_assign(stmt)
        if isinstance(stmt, ast.AnnAssign):
            return self._visit_ann_assign(stmt)
        if isinstance(stmt, ast.Expr):
            return self._visit_expr_stmt(stmt)
        if isinstance(stmt, ast.If):
            return self._visit_if(stmt)
        if isinstance(stmt, ast.Return):
            return self._visit_return(stmt)
        raise SynthesisError(
            f"Non-synthesizable statement {type(stmt).__name__}"
            + (f" at line {stmt.lineno}" if hasattr(stmt, 'lineno') else "")
        )

    # ------------------------------------------------------------------
    # Testbench-mode statement handlers
    # ------------------------------------------------------------------

    def _visit_stmt_tb(self, stmt: ast.stmt) -> HwStmt | None:
        """Match TB-only stmt shapes. Return ``None`` to fall through to
        the kernel-mode handler (for stmts that have identical semantics in
        both modes, e.g. ``return``, ``if``, bare docstring constants).
        """
        # Process (yielding) forms of TB ops.  A single-process ``SeqTB.main()``
        # may ``yield`` to model timing; those yields carry no C-sim meaning, so
        # a ``yield from`` of a TB call lowers to the SAME IR as its synchronous
        # twin (byte-identical C++):
        #   ``[y =] yield from dut.run_once_sim(...)`` == ``dut.run_once(...)``
        #   ``yield from ep.write(data)``             == ``ep.push(data)``
        #   ``x = yield from ep.get(Schema[, count])``== ``ep.pop(x)``
        # A bare ``yield self.timeout(...)`` (a @sim_only latency model) is
        # stripped by the shared kernel-mode handler (`_visit_expr_stmt`), which
        # this method deliberately falls through to.
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.YieldFrom):
            inner = stmt.value.value
            if isinstance(inner, ast.Call):
                result = self._visit_tb_yield_from(inner, [], stmt)
                if result is not None:
                    return result
        if (
            isinstance(stmt, (ast.Assign, ast.AnnAssign))
            and isinstance(stmt.value, ast.YieldFrom)
            and isinstance(stmt.value.value, ast.Call)
        ):
            targets = (
                list(stmt.targets)
                if isinstance(stmt, ast.Assign)
                else [stmt.target]
            )
            result = self._visit_tb_yield_from(stmt.value.value, targets, stmt)
            if result is not None:
                return result

        # `name = <ClassRef>(**kwargs)` — DUT / schema / MemModel binding,
        # or `name = <mem>.read_array(...)` — m_axi read-back.
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
        ):
            local_name = stmt.targets[0].id
            bind = self._try_dut_bind(local_name, stmt.value, stmt)
            if bind is not None:
                return bind
            mem_bind = self._try_mem_bind(local_name, stmt.value, stmt)
            if mem_bind is not None:
                return mem_bind
            schema_bind = self._try_schema_bind(local_name, stmt.value, stmt)
            if schema_bind is not None:
                return schema_bind
            file_read = self._try_schema_file_read(local_name, stmt.value, stmt)
            if file_read is not None:
                return file_read
            mem_read = self._try_mem_read_array(local_name, stmt.value, stmt)
            if mem_read is not None:
                return mem_read

        # `cmd.addr = <mem>.alloc_array(buf, ElemT, count=...)` — alloc + populate.
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Attribute)
            and isinstance(stmt.targets[0].value, ast.Name)
            and isinstance(stmt.value, ast.Call)
        ):
            alloc = self._try_mem_alloc_array(stmt.targets[0], stmt.value, stmt)
            if alloc is not None:
                return alloc
        # Expression-statement calls (no return assignment): the bulk of TB
        # mode lives here — push/pop, file IO, regmap helpers, dut.run().
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            result = self._visit_tb_call(stmt.value, stmt)
            if result is not None:
                return result
        return None

    # ------------------------------------------------------------------
    # TB-mode call dispatch
    # ------------------------------------------------------------------

    def _visit_tb_call(
        self, call: ast.Call, parent: ast.stmt,
    ) -> HwStmt | None:
        """Lower a bare ``<chain>(...)`` expression statement to TB IR."""
        if not isinstance(call.func, ast.Attribute):
            return None
        attr = call.func.attr
        receiver_chain = call.func.value

        # `dut.run()` / `dut.run(mem=<memlocal>)` — kernel call.
        if (
            attr == 'run'
            and isinstance(receiver_chain, ast.Name)
            and receiver_chain.id in self._duts
        ):
            if call.args:
                raise SynthesisError(
                    f"dut.run() takes no positional arguments (line {parent.lineno})"
                )
            mem_local: str | None = None
            for kw in call.keywords:
                if kw.arg == 'mem' and isinstance(kw.value, ast.Name):
                    if kw.value.id not in self._mems:
                        raise SynthesisError(
                            f"dut.run(mem={kw.value.id}) — '{kw.value.id}' is not a "
                            f"bound MemModel local (line {parent.lineno})"
                        )
                    mem_local = kw.value.id
                else:
                    raise SynthesisError(
                        f"dut.run() only accepts mem=<MemModel local> "
                        f"(line {parent.lineno})"
                    )
            return KernelCallStmt(local_name=receiver_chain.id, mem_local=mem_local)

        # `dut.run_once(dut.regmap.get("f0"), dut.regmap.get("f1"), ...)` — the invocation form
        # (Phase 5b).  It lowers to the SAME kernel call as `dut.run()`: run_once's positional args
        # reference the *input* regmap fields (RW/W) by name, in declaration order, so the field
        # locals (already declared + populated) are passed straight through and the emitted call is
        # identical.  We map by field name/access, never by naive arg position.
        if (
            attr == 'run_once'
            and isinstance(receiver_chain, ast.Name)
            and receiver_chain.id in self._duts
        ):
            if call.keywords:
                raise SynthesisError(
                    f"dut.run_once() takes only positional field args "
                    f"(line {parent.lineno})"
                )
            self._validate_run_once_args(receiver_chain.id, call.args, parent)
            return KernelCallStmt(local_name=receiver_chain.id, mem_local=None)

        # `<schema_local>.read_uint32_file*(...)` / `.write_uint32_file*(...)`
        if (
            attr in _TB_FILE_IO_METHODS
            and isinstance(receiver_chain, ast.Name)
            and receiver_chain.id in self._tb_locals
        ):
            return self._make_file_io_stmt(
                receiver_chain.id, attr, call, parent,
            )

        # `dut.<ep>.push(...)`, `.push_array(...)`, `.pop(...)`, `.pop_array(...)`
        if (
            (attr in _TB_STREAM_PUSH_METHODS or attr in _TB_STREAM_POP_METHODS)
            and isinstance(receiver_chain, ast.Attribute)
            and isinstance(receiver_chain.value, ast.Name)
            and receiver_chain.value.id in self._duts
        ):
            return self._make_stream_io_stmt(
                dut_local=receiver_chain.value.id,
                endpoint_attr=receiver_chain.attr,
                method_attr=attr,
                call=call,
                parent=parent,
            )

        # `dut.regmap.read_uint32_file_array(field, path, count=...)`
        # `dut.regmap.write_status_json(path, fields=[...])`
        if (
            isinstance(receiver_chain, ast.Attribute)
            and receiver_chain.attr == 'regmap'
            and isinstance(receiver_chain.value, ast.Name)
            and receiver_chain.value.id in self._duts
        ):
            return self._make_regmap_call_stmt(
                dut_local=receiver_chain.value.id,
                method_attr=attr,
                call=call,
                parent=parent,
            )

        return None

    def _visit_tb_yield_from(
        self,
        call: ast.Call,
        targets: list[ast.expr],
        parent: ast.stmt,
    ) -> HwStmt | None:
        """Lower the *process* (yielding) form of a TB op to the SAME IR as its
        synchronous twin.

        Handles ``[y =] yield from dut.run_once_sim(...)`` (a
        :class:`KernelCallStmt`, identical to ``dut.run_once(...)``, plus an
        optional return-capture) and ``yield from ep.write(data)`` /
        ``x = yield from ep.get(Schema[, count=...])`` (a
        :class:`TbStreamIOStmt`, identical to ``ep.push``/``ep.pop``).  Returns
        ``None`` if *call* is not a recognized TB process op, so the caller can
        fall through to the kernel-mode handlers.
        """
        if not isinstance(call.func, ast.Attribute):
            return None
        attr = call.func.attr
        receiver = call.func.value

        # `[y =] yield from dut.run_once_sim(...)` — the sim-driven invocation.
        # Lowers 1:1 to the same kernel call as `dut.run_once(...)` (Phase 5b):
        # the positional args reference the input regmap fields by name/access,
        # and the return (if captured) binds to the DUT's *output* fields — an
        # alias that emits no C++ (the out-param locals are already declared by
        # the DutBindStmt and passed by the kernel call), so run_once and
        # run_once_sim are byte-identical.
        if (
            attr == 'run_once_sim'
            and isinstance(receiver, ast.Name)
            and receiver.id in self._duts
        ):
            if call.keywords:
                raise SynthesisError(
                    f"dut.run_once_sim() takes only positional field args "
                    f"(line {parent.lineno})"
                )
            self._validate_run_once_args(receiver.id, call.args, parent)
            self._bind_run_once_outputs(receiver.id, targets, parent)
            return KernelCallStmt(local_name=receiver.id, mem_local=None)

        # `yield from dut.<ep>.write(...)` / `x = yield from dut.<ep>.get(...)`
        # — the process forms of push/pop.
        if (
            attr in ('write', 'get')
            and isinstance(receiver, ast.Attribute)
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id in self._duts
        ):
            return self._make_stream_proc_stmt(
                dut_local=receiver.value.id,
                endpoint_attr=receiver.attr,
                method_attr=attr,
                call=call,
                targets=targets,
                parent=parent,
            )
        return None

    # ------------------------------------------------------------------
    # TB-mode helpers
    # ------------------------------------------------------------------

    def _try_schema_bind(
        self,
        local_name: str,
        call_node: ast.Call,
        parent_stmt: ast.stmt,
    ) -> SchemaBindStmt | None:
        """If ``call_node`` constructs a ``DataSchema`` subclass with no
        arguments, return a ``SchemaBindStmt`` and record the binding."""
        from waveflow.hw.dataschema import DataSchema

        cls = self._resolve_tb_class(call_node.func)
        if not (isinstance(cls, type) and issubclass(cls, DataSchema)):
            return None
        if call_node.args or call_node.keywords:
            raise SynthesisError(
                f"DataSchema construction in TB mode takes no arguments "
                f"(line {parent_stmt.lineno})"
            )
        self._tb_locals[local_name] = cls
        return SchemaBindStmt(local_name=local_name, schema_class=cls)

    def _try_schema_file_read(
        self,
        local_name: str,
        call_node: ast.Call,
        parent_stmt: ast.stmt,
    ) -> "HwStmt | None":
        """``x = <Schema>().read_uint32_file(<path>)`` — read a uint32-packed input
        file into a plain TB **local** (Stage 2b).

        This is the standard schema file-IO spelling
        (:meth:`~waveflow.hw.dataschema.DataSchema.read_uint32_file`, the same
        ``PolyCmdHdr().read_uint32_file(path)`` a build script uses), so it needs **no**
        new runtime: a run of ``main()`` gets a real schema instance back, and
        ``dut.run_once_sim(x, a, b)`` passes those values straight in.  It replaces the
        input *round-trip* (``regmap.read_uint32_file("x", p)`` then ``regmap.get("x")``).

        **v1 scope — the input-side mirror of the Stage-1 output capture.**  *local_name*
        must name an **input** regmap field (``RW``/``W``, non-``is_vitis_auto``) of a bound
        DUT, and ``<Schema>`` must be that field's schema.  Because the C++ local for that
        field is already declared by the ``DutBindStmt`` (exactly as
        ``y = yield from dut.run_once_sim(...)`` aliases the *output* field local and emits
        no declaration of its own), the read lowers to the same
        :class:`~waveflow.hw.hwstmt.TbRegmapFileReadStmt` the round-trip form emitted — so
        the generated C++ is unchanged while the Python drops the round-trip.  Reads into a
        free-standing local (one that names no input field) would need a fresh C++ decl plus
        arg-carrying kernel calls; that is a follow-on.

        Returns ``None`` if *call_node* is not this shape, so the caller falls through.
        """
        from waveflow.hw.dataschema import DataSchema

        if not (
            isinstance(call_node.func, ast.Attribute)
            and call_node.func.attr == 'read_uint32_file'
            and isinstance(call_node.func.value, ast.Call)
        ):
            return None
        ctor = call_node.func.value
        cls = self._resolve_tb_class(ctor.func)
        if not (isinstance(cls, type) and issubclass(cls, DataSchema)):
            return None
        if ctor.args or ctor.keywords:
            raise SynthesisError(
                f"<Schema>().read_uint32_file(path): the schema construction takes no "
                f"arguments (line {parent_stmt.lineno})"
            )
        if len(call_node.args) != 1 or call_node.keywords:
            raise SynthesisError(
                f"<Schema>().read_uint32_file(path) takes exactly one path argument "
                f"(line {parent_stmt.lineno})"
            )
        dut_local = self._resolve_input_field_local(local_name, cls, parent_stmt)
        # Record the alias for run_once/run_once_sim arg validation.  Deliberately NOT
        # registered in ``_tb_locals``: that table means "a schema local with its own C++
        # storage" (a struct/raw-array a push/pop or write_uint32_file can target), which
        # this scalar field alias is not.
        self._tb_field_locals[local_name] = (dut_local, local_name)
        return TbRegmapFileReadStmt(
            dut_local=dut_local,
            field_name=local_name,
            path=call_node.args[0],
            count=None,
        )

    def _resolve_input_field_local(
        self, local_name: str, cls: type, parent_stmt: ast.stmt,
    ) -> str:
        """Resolve *local_name* to the bound DUT whose **input** regmap field it names.

        Returns the DUT's local name.  Raises :class:`SynthesisError` when no bound DUT
        (or more than one) has an input field so named, or when *cls* is not that field's
        declared schema — the read must fill the field it is passed to.
        """
        from waveflow.hw.regmap import RegAccess

        matches: list[tuple[str, object]] = []
        for dut_local in self._duts:
            regmap = self._dut_regmap_or_none(dut_local)
            if regmap is None:
                continue
            fld = regmap._fields.get(local_name)
            if (
                fld is not None
                and not fld.is_vitis_auto
                and fld.access in (RegAccess.RW, RegAccess.W)
            ):
                matches.append((dut_local, fld))
        if not matches:
            raise SynthesisError(
                f"'{local_name} = {cls.__name__}().read_uint32_file(...)': a testbench "
                f"file read must name an input regmap field (RW/W) of a bound DUT — no "
                f"bound DUT has an input field '{local_name}' (line {parent_stmt.lineno})"
            )
        if len(matches) > 1:
            duts = ", ".join(d for d, _ in matches)
            raise SynthesisError(
                f"'{local_name} = {cls.__name__}().read_uint32_file(...)': input field "
                f"'{local_name}' is ambiguous — it is declared by DUTs {duts} "
                f"(line {parent_stmt.lineno})"
            )
        dut_local, fld = matches[0]
        if fld.schema is not cls:
            raise SynthesisError(
                f"'{local_name} = {cls.__name__}().read_uint32_file(...)': input field "
                f"'{local_name}' is declared as {fld.schema.__name__}, not {cls.__name__} "
                f"(line {parent_stmt.lineno})"
            )
        return dut_local

    def _try_mem_bind(
        self,
        local_name: str,
        call_node: ast.Call,
        parent_stmt: ast.stmt,
    ) -> "HwStmt | None":
        """If ``call_node`` constructs a ``MemModel``, return a
        ``MemBindStmt`` (flat array + MemMgr).  ``None`` otherwise."""
        from waveflow.hw.hwstmt import MemBindStmt
        from waveflow.hw.memory import MemModel

        cls = self._resolve_tb_class(call_node.func)
        if not (isinstance(cls, type) and issubclass(cls, MemModel)):
            return None
        if call_node.args:
            raise SynthesisError(
                f"MemModel construction must use keyword arguments only "
                f"(line {parent_stmt.lineno})"
            )
        kwargs: dict[str, object] = {}
        for kw in call_node.keywords:
            if kw.arg is None:
                raise SynthesisError(
                    f"MemModel construction does not accept **kwargs "
                    f"(line {parent_stmt.lineno})"
                )
            kwargs[kw.arg] = self._eval_tb_literal(kw.value, parent_stmt)
        word_size = int(kwargs.get('word_size', 32))
        nwords_tot = kwargs.get('nwords_tot')
        if nwords_tot is None:
            raise SynthesisError(
                f"MemModel in a testbench must declare nwords_tot "
                f"(the static array size) (line {parent_stmt.lineno})"
            )
        self._mems[local_name] = (word_size, int(nwords_tot))
        return MemBindStmt(
            local_name=local_name,
            nwords_tot=int(nwords_tot),
            word_size=word_size,
        )

    def _try_mem_alloc_array(
        self,
        target: ast.Attribute,
        call: ast.Call,
        parent: ast.stmt,
    ) -> "HwStmt | None":
        """``cmd.addr = mem.alloc_array(buf, ElemT, count=expr)``."""
        from waveflow.hw.hwstmt import MemAllocArrayStmt

        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == 'alloc_array'
                and isinstance(func.value, ast.Name)
                and func.value.id in self._mems):
            return None
        mem_local = func.value.id
        target_local = target.value.id  # type: ignore[union-attr]
        target_field = target.attr
        if len(call.args) != 2:
            raise SynthesisError(
                f"mem.alloc_array(buf, ElemT, count=...) requires two positional "
                f"args (line {parent.lineno})"
            )
        buf_node, elem_node = call.args
        if not (isinstance(buf_node, ast.Name) and buf_node.id in self._tb_locals):
            raise SynthesisError(
                f"mem.alloc_array first arg must be a bound buffer local "
                f"(line {parent.lineno})"
            )
        elem_type = self._resolve_tb_class(elem_node)
        if not isinstance(elem_type, type):
            raise SynthesisError(
                f"mem.alloc_array element type must resolve to a schema class "
                f"(line {parent.lineno})"
            )
        count = self._extract_count_kwarg(call, 'alloc_array', parent)
        return MemAllocArrayStmt(
            mem_local=mem_local,
            target_local=target_local,
            target_field=target_field,
            src_local=buf_node.id,
            elem_type=elem_type,
            count=count,
        )

    def _try_mem_read_array(
        self,
        local_name: str,
        call: ast.Call,
        parent: ast.stmt,
    ) -> "HwStmt | None":
        """``out = mem.read_array(addr_expr, ElemT, count=expr)``."""
        from waveflow.hw.hwstmt import MemReadArrayStmt

        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == 'read_array'
                and isinstance(func.value, ast.Name)
                and func.value.id in self._mems):
            return None
        mem_local = func.value.id
        if len(call.args) != 2:
            raise SynthesisError(
                f"mem.read_array(addr, ElemT, count=...) requires two positional "
                f"args (line {parent.lineno})"
            )
        addr_node, elem_node = call.args
        elem_type = self._resolve_tb_class(elem_node)
        if not isinstance(elem_type, type):
            raise SynthesisError(
                f"mem.read_array element type must resolve to a schema class "
                f"(line {parent.lineno})"
            )
        count = self._extract_count_kwarg(call, 'read_array', parent)
        # Register the output buffer local so a following write_uint32_file_array
        # recognizes it.  Sized at the backing memory's nwords_tot (static array).
        from waveflow.hw.dataschema import DataArray
        _word_size, mem_nwords = self._mems[mem_local]
        out_cls = DataArray.specialize(
            element_type=elem_type,
            max_shape=(mem_nwords,),
            static=True,
        )
        self._tb_locals[local_name] = out_cls
        return MemReadArrayStmt(
            mem_local=mem_local,
            target_local=local_name,
            addr=addr_node,
            elem_type=elem_type,
            count=count,
        )

    def _eval_tb_literal(self, node: ast.expr, parent: ast.stmt) -> object:
        """Resolve a TB construction kwarg value: literal constant or a
        module-global name (e.g. ``nwords_tot=MAX_N``)."""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            method = getattr(self._comp, self._method_name)
            globs = getattr(method, '__globals__', {})
            if node.id in globs:
                return globs[node.id]
        raise SynthesisError(
            f"MemModel kwarg must be a literal or module global "
            f"(line {getattr(parent, 'lineno', '?')})"
        )

    def _make_file_io_stmt(
        self,
        target_local: str,
        method: str,
        call: ast.Call,
        parent: ast.stmt,
    ) -> TbFileIOStmt:
        """Build a :class:`TbFileIOStmt` from a ``<local>.<method>(...)`` call."""
        is_array = method.endswith('_array')
        is_write = method.startswith('write_')
        if not call.args:
            raise SynthesisError(
                f"{method}() requires a path argument (line {parent.lineno})"
            )
        path = call.args[0]
        count: object | None = None
        if is_array:
            count = self._extract_count_kwarg(call, method, parent)
            if len(call.args) > 1:
                raise SynthesisError(
                    f"{method}() takes path + count=...; extra positional "
                    f"args at line {parent.lineno}"
                )
        else:
            if len(call.args) > 1 or call.keywords:
                raise SynthesisError(
                    f"{method}() takes only a path argument "
                    f"(line {parent.lineno})"
                )
        return TbFileIOStmt(
            target_local=target_local,
            path=path,
            is_write=is_write,
            is_array=is_array,
            count=count,
        )

    def _make_stream_io_stmt(
        self,
        dut_local: str,
        endpoint_attr: str,
        method_attr: str,
        call: ast.Call,
        parent: ast.stmt,
    ) -> TbStreamIOStmt:
        """Build a :class:`TbStreamIOStmt` from a ``dut.<ep>.<method>(...)`` call."""
        is_pop = method_attr in _TB_STREAM_POP_METHODS
        is_array = method_attr.endswith('_array')
        if not call.args:
            raise SynthesisError(
                f"{method_attr}() requires a value argument "
                f"(line {parent.lineno})"
            )
        value_node = call.args[0]
        if not isinstance(value_node, ast.Name):
            raise SynthesisError(
                f"{method_attr}() value must be a local-name reference "
                f"(line {parent.lineno})"
            )
        value_local = value_node.id
        if value_local not in self._tb_locals:
            raise SynthesisError(
                f"{method_attr}({value_local}, ...) — '{value_local}' is "
                f"not a bound schema local (line {parent.lineno})"
            )
        count: object | None = None
        if is_array:
            count = self._extract_count_kwarg(call, method_attr, parent)
            if len(call.args) > 1:
                raise SynthesisError(
                    f"{method_attr}() takes value + count=...; extra "
                    f"positional args at line {parent.lineno}"
                )
        else:
            if len(call.args) > 1 or call.keywords:
                raise SynthesisError(
                    f"{method_attr}() takes only a value argument "
                    f"(line {parent.lineno})"
                )
        return TbStreamIOStmt(
            dut_local=dut_local,
            endpoint_attr=endpoint_attr,
            value_local=value_local,
            is_pop=is_pop,
            is_array=is_array,
            count=count,
        )

    def _make_regmap_call_stmt(
        self,
        dut_local: str,
        method_attr: str,
        call: ast.Call,
        parent: ast.stmt,
    ) -> HwStmt | None:
        """Handle ``dut.regmap.<method>(...)`` calls in TB mode."""
        if method_attr == 'read_uint32_file':
            if len(call.args) != 2 or call.keywords:
                raise SynthesisError(
                    f"dut.regmap.read_uint32_file(field, path) "
                    f"requires exactly two positional args "
                    f"(line {parent.lineno})"
                )
            field_node = call.args[0]
            if not (isinstance(field_node, ast.Constant)
                    and isinstance(field_node.value, str)):
                raise SynthesisError(
                    f"dut.regmap.read_uint32_file(field, ...): "
                    f"'field' must be a string literal (line {parent.lineno})"
                )
            return TbRegmapFileReadStmt(
                dut_local=dut_local,
                field_name=field_node.value,
                path=call.args[1],
                count=None,
            )
        if method_attr == 'read_uint32_file_array':
            if len(call.args) != 2:
                raise SynthesisError(
                    f"dut.regmap.read_uint32_file_array(field, path, count=...) "
                    f"requires exactly two positional args "
                    f"(line {parent.lineno})"
                )
            field_node = call.args[0]
            if not (isinstance(field_node, ast.Constant)
                    and isinstance(field_node.value, str)):
                raise SynthesisError(
                    f"dut.regmap.read_uint32_file_array(field, ...): "
                    f"'field' must be a string literal (line {parent.lineno})"
                )
            count = self._extract_count_kwarg(call, method_attr, parent)
            return TbRegmapFileReadStmt(
                dut_local=dut_local,
                field_name=field_node.value,
                path=call.args[1],
                count=count,
            )
        if method_attr == 'write_status_json':
            if len(call.args) != 1:
                raise SynthesisError(
                    f"dut.regmap.write_status_json(path, fields=[...]) "
                    f"requires exactly one positional arg "
                    f"(line {parent.lineno})"
                )
            fields_kw = next(
                (kw for kw in call.keywords if kw.arg == 'fields'), None,
            )
            if fields_kw is None or not isinstance(fields_kw.value, ast.List):
                raise SynthesisError(
                    f"dut.regmap.write_status_json(...) requires fields=[...]"
                    f" as a list literal (line {parent.lineno})"
                )
            names: list[str] = []
            for elt in fields_kw.value.elts:
                if not (isinstance(elt, ast.Constant)
                        and isinstance(elt.value, str)):
                    raise SynthesisError(
                        f"write_status_json fields must be string literals "
                        f"(line {parent.lineno})"
                    )
                names.append(elt.value)
            names = self._filter_vitis_auto_fields(dut_local, names)
            return TbStatusJsonStmt(
                dut_local=dut_local,
                path=call.args[0],
                field_names=names,
            )
        return None

    def _filter_vitis_auto_fields(
        self,
        dut_local: str,
        field_names: list[str],
    ) -> list[str]:
        """Drop ``is_vitis_auto`` fields from a ``write_status_json`` list.

        Vitis HLS auto-generates ``ap_start``/``ap_done`` inside the
        ``s_axilite`` control register — they are not C++ kernel
        parameters, so the generated TB cannot read them as locals.
        The Python side records them naturally; matching the symmetric
        Python shape just means listing them here too. This filter lets
        users write the idiomatic symmetric list and lowers it to the
        subset the C++ TB can actually emit.
        """
        regmap = self._dut_regmap_or_none(dut_local)
        if regmap is None:
            return field_names
        kept: list[str] = []
        skipped: list[str] = []
        for n in field_names:
            fld = regmap._fields.get(n)
            if fld is not None and getattr(fld, 'is_vitis_auto', False):
                skipped.append(n)
            else:
                kept.append(n)
        if skipped:
            _log.debug(
                "write_status_json on '%s': dropped is_vitis_auto fields %s "
                "(no C++ local in the generated TB)",
                dut_local, skipped,
            )
        return kept

    def _dut_regmap_or_none(self, dut_local: str):
        """Return the ``VitisRegMap`` of the DUT bound to ``dut_local``, or
        ``None`` if the DUT has no regmap.

        The DUT class is held in ``self._duts`` as the class itself; this
        helper instantiates it lazily with ``name``/``sim`` placeholders
        so we can inspect the regmap's field declarations at parse time.
        """
        cls = self._duts.get(dut_local)
        if cls is None or not isinstance(cls, type):
            return None
        from waveflow.build.elaborate import elaborate
        try:
            comp = elaborate(cls)
        except Exception:
            return None
        return getattr(comp, 'regmap', None)

    def _run_once_arg_field(self, dut_local: str, arg: ast.expr) -> str | None:
        """Return the input regmap field of *dut_local* that a ``run_once`` positional
        *arg* names, else ``None``.

        Two accepted spellings, both naming the field whose local is passed straight through:

        - ``<dut>.regmap.get("<field>")`` — the field read back out of the DUT;
        - ``<field>`` — a plain TB local bound by ``<field> = <Schema>().read_uint32_file(path)``
          (Stage 2b), which drops the round-trip.  Only locals registered in
          ``_tb_field_locals`` qualify: those alias the DUT's input-field C++ local, so passing
          one emits the identical kernel-call arg.  The alias must belong to the DUT being
          invoked — a local naming *another* DUT's field is not this DUT's arg.
        """
        if isinstance(arg, ast.Name) and arg.id in self._tb_field_locals:
            owner, field = self._tb_field_locals[arg.id]
            return field if owner == dut_local else None
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == 'get'
            and isinstance(arg.func.value, ast.Attribute)
            and arg.func.value.attr == 'regmap'
            and isinstance(arg.func.value.value, ast.Name)
            and arg.func.value.value.id in self._duts
            and len(arg.args) == 1
            and not arg.keywords
            and isinstance(arg.args[0], ast.Constant)
            and isinstance(arg.args[0].value, str)
        ):
            return arg.args[0].value
        return None

    def _validate_run_once_args(
        self, dut_local: str, args: list[ast.expr], parent: ast.stmt,
    ) -> None:
        """Check ``dut.run_once(...)`` args reference the DUT's input regmap fields by name.

        Phase-5b scope (pure regmap-scalar): each positional arg must be
        ``dut.regmap.get("<field>")`` naming the *input* field (``RW``/``W``, non-``is_vitis_auto``)
        in that position, taken in declaration order.  Binding is by **name/access**, not naive
        position — so an output field interleaved among the inputs cannot be mis-mapped.  Output
        (``R``) fields are not passed positionally; the generated call carries them as out-params
        (identical to ``dut.run()``).  Richer forms — literal/local args needing an input-field
        assignment, a captured ``z = dut.run_once(...)`` return, or stream I/O — are follow-ons.
        """
        from waveflow.hw.regmap import RegAccess

        regmap = self._dut_regmap_or_none(dut_local)
        if regmap is None:
            raise SynthesisError(
                f"dut.run_once() on '{dut_local}': DUT has no regmap to invoke "
                f"(line {parent.lineno})"
            )
        inputs = [
            name for name, fld in regmap._fields.items()
            if not fld.is_vitis_auto and fld.access in (RegAccess.RW, RegAccess.W)
        ]
        if len(args) != len(inputs):
            raise SynthesisError(
                f"dut.run_once() takes {len(inputs)} input arg(s) {inputs}, got {len(args)} "
                f"(line {parent.lineno})"
            )
        for i, (arg, field) in enumerate(zip(args, inputs)):
            ref = self._run_once_arg_field(dut_local, arg)
            if ref is None:
                raise SynthesisError(
                    f"dut.run_once() arg {i} must be dut.regmap.get(\"{field}\") or the local "
                    f"'{field}' bound by '{field} = <Schema>().read_uint32_file(...)' — a "
                    f"run_once TB arg references its input regmap field by name "
                    f"(line {parent.lineno})"
                )
            if ref != field:
                raise SynthesisError(
                    f"dut.run_once() arg {i} references field '{ref}', but the input field in that "
                    f"position is '{field}' — args must match the input fields in declaration order "
                    f"(line {parent.lineno})"
                )

    def _dut_output_fields(self, dut_local: str) -> list[tuple[str, object]]:
        """Return the DUT's *output* regmap fields — ``R`` access, non
        ``is_vitis_auto`` — as ``(name, schema)`` in declaration order.

        These are the fields the C++ kernel carries as out-params and the
        return value of :meth:`~waveflow.hw.hw_hostactivated.HostActivated.run_once_sim`.
        """
        from waveflow.hw.regmap import RegAccess

        regmap = self._dut_regmap_or_none(dut_local)
        if regmap is None:
            return []
        return [
            (name, fld.schema)
            for name, fld in regmap._fields.items()
            if not fld.is_vitis_auto and fld.access is RegAccess.R
        ]

    def _capture_target_names(
        self, targets: list[ast.expr], parent: ast.stmt,
    ) -> list[str]:
        """Flatten assignment targets (a single ``Name`` or a ``Tuple`` of
        ``Name``s) to a list of local names.  Raises on any other shape."""
        names: list[str] = []
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Tuple):
                for elt in t.elts:
                    if not isinstance(elt, ast.Name):
                        raise SynthesisError(
                            f"run_once_sim() return must be captured into plain "
                            f"local names (line {parent.lineno})"
                        )
                    names.append(elt.id)
            else:
                raise SynthesisError(
                    f"run_once_sim() return must be captured into a local name "
                    f"(line {parent.lineno})"
                )
        return names

    def _bind_run_once_outputs(
        self, dut_local: str, targets: list[ast.expr], parent: ast.stmt,
    ) -> None:
        """Bind the capture targets of ``y = yield from dut.run_once_sim(...)``
        to the DUT's output regmap fields (in declaration order).

        Emits **no** C++: the output-field locals are already declared by the
        ``DutBindStmt`` and passed as out-params in the kernel call, so
        capturing the return only makes the name available to later statements.
        This is what keeps ``run_once`` and ``run_once_sim`` byte-identical.
        """
        if not targets:
            return
        names = self._capture_target_names(targets, parent)
        outputs = self._dut_output_fields(dut_local)
        if len(names) != len(outputs):
            raise SynthesisError(
                f"run_once_sim() on '{dut_local}' returns {len(outputs)} output "
                f"field(s) {[n for n, _ in outputs]}, but {len(names)} capture "
                f"target(s) {names} were given (line {parent.lineno})"
            )
        for local_name, (_fname, schema) in zip(names, outputs):
            self._tb_locals[local_name] = schema

    def _make_stream_proc_stmt(
        self,
        dut_local: str,
        endpoint_attr: str,
        method_attr: str,
        call: ast.Call,
        targets: list[ast.expr],
        parent: ast.stmt,
    ) -> HwStmt:
        """Lower ``ep.write(...)`` / ``ep.get(...)`` (the process forms) to the
        SAME IR as ``ep.push(...)`` / ``ep.pop(...)``.

        ``write``/``get`` distinguish scalar vs array by the presence of a
        ``count=`` keyword (``push`` vs ``push_array``); ``get`` supplies the
        output *schema class* as its first positional arg, so it expands to a
        declaration (the ``SchemaBindStmt`` a synchronous ``pop`` gets from a
        prior ``x = Schema()``) plus the pop itself.
        """
        from waveflow.hw.dataschema import DataSchema

        has_count = any(kw.arg == 'count' for kw in call.keywords)
        if method_attr == 'get':
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                raise SynthesisError(
                    f"ep.get(...) return must be captured into a single local "
                    f"(line {parent.lineno})"
                )
            local_name = targets[0].id
            if not call.args:
                raise SynthesisError(
                    f"ep.get(Schema, ...) requires a schema-class argument "
                    f"(line {parent.lineno})"
                )
            cls = self._resolve_tb_class(call.args[0])
            if not (isinstance(cls, type) and issubclass(cls, DataSchema)):
                raise SynthesisError(
                    f"ep.get(...) first arg must be a DataSchema subclass "
                    f"(line {parent.lineno})"
                )
            count = (
                self._extract_count_kwarg(call, 'get', parent)
                if has_count else None
            )
            # Register + declare the output local (mirrors `x = Schema()`), then
            # pop into it — two IR statements, emitted inline (a SeqStmt joins
            # with '\n', no extra scope), byte-identical to the pop twin.
            self._tb_locals[local_name] = cls
            bind = SchemaBindStmt(local_name=local_name, schema_class=cls)
            io = TbStreamIOStmt(
                dut_local=dut_local,
                endpoint_attr=endpoint_attr,
                value_local=local_name,
                is_pop=True,
                is_array=has_count,
                count=count,
            )
            return SeqStmt(stmts=[bind, io])

        # method_attr == 'write'
        if targets:
            raise SynthesisError(
                f"ep.write(...) does not return a value to capture "
                f"(line {parent.lineno})"
            )
        if not call.args or not isinstance(call.args[0], ast.Name):
            raise SynthesisError(
                f"ep.write(value) value must be a local-name reference "
                f"(line {parent.lineno})"
            )
        value_local = call.args[0].id
        if value_local not in self._tb_locals:
            raise SynthesisError(
                f"ep.write({value_local}, ...) — '{value_local}' is not a bound "
                f"schema local (line {parent.lineno})"
            )
        count = (
            self._extract_count_kwarg(call, 'write', parent)
            if has_count else None
        )
        return TbStreamIOStmt(
            dut_local=dut_local,
            endpoint_attr=endpoint_attr,
            value_local=value_local,
            is_pop=False,
            is_array=has_count,
            count=count,
        )

    def _extract_count_kwarg(
        self,
        call: ast.Call,
        method_name: str,
        parent: ast.stmt,
    ) -> object:
        """Pull the required ``count=<expr>`` keyword from an array-mode call."""
        for kw in call.keywords:
            if kw.arg == 'count':
                return kw.value
        raise SynthesisError(
            f"{method_name}() requires count=<int> (line {parent.lineno})"
        )

    def _try_dut_bind(
        self,
        local_name: str,
        call_node: ast.Call,
        parent_stmt: ast.stmt,
    ) -> DutBindStmt | None:
        """If ``call_node`` constructs a ``HwModule`` subclass, return a
        matching ``DutBindStmt``; otherwise ``None`` so the dispatcher can
        try other patterns.
        """
        from waveflow.hw.hw_module import HwModule

        cls = self._resolve_tb_class(call_node.func)
        if not (isinstance(cls, type) and issubclass(cls, HwModule)):
            return None
        if call_node.args:
            raise SynthesisError(
                f"DUT construction must use keyword arguments only "
                f"(line {parent_stmt.lineno})"
            )
        kwargs: dict[str, object] = {}
        for kw in call_node.keywords:
            if kw.arg is None:
                raise SynthesisError(
                    f"DUT construction does not accept **kwargs "
                    f"(line {parent_stmt.lineno})"
                )
            if not isinstance(kw.value, ast.Constant):
                raise SynthesisError(
                    f"DUT construction kwargs must be literal constants "
                    f"(line {parent_stmt.lineno})"
                )
            kwargs[kw.arg] = kw.value.value
        self._duts[local_name] = cls
        return DutBindStmt(
            local_name=local_name, comp_class=cls, kwargs=kwargs,
        )

    def _resolve_tb_class(self, func_node: ast.expr) -> object | None:
        """Resolve a class reference in the testbench's ``main`` globals.

        Supports bare names (``PolyAccel``) and attribute chains
        rooted in a global (``mod.PolyAccel``).
        """
        method = getattr(self._comp, self._method_name)
        globs = getattr(method, '__globals__', {})
        if isinstance(func_node, ast.Name):
            return globs.get(func_node.id)
        if isinstance(func_node, ast.Attribute):
            path: list[str] = []
            node: ast.expr = func_node
            while isinstance(node, ast.Attribute):
                path.append(node.attr)
                node = node.value
            if not isinstance(node, ast.Name):
                return None
            obj = globs.get(node.id)
            for attr in reversed(path):
                if obj is None:
                    return None
                obj = getattr(obj, attr, None)
            return obj
        return None

    # ------------------------------------------------------------------
    # Individual statement handlers
    # ------------------------------------------------------------------

    def _visit_while(self, node: ast.While) -> WhileStmt:
        if not (isinstance(node.test, ast.Constant) and node.test.value is True):
            raise SynthesisError(
                f"Only 'while True:' is synthesizable (line {node.lineno})"
            )
        if node.orelse:
            raise SynthesisError(
                f"'while/else' is not synthesizable (line {node.lineno})"
            )
        body_stmts = self._visit_stmts(node.body)
        return WhileStmt(body=SeqStmt(stmts=body_stmts))

    def _visit_assign(self, node: ast.Assign) -> HwStmt:
        # x = yield from self.ep.method(...)
        if isinstance(node.value, ast.YieldFrom):
            return self._make_call_with_binding(node.value.value, node.targets)
        # a, b = self.hook(var1, var2)   — direct call, no yield from
        if isinstance(node.value, ast.Call):
            method = self._resolve_method(node.value.func)
            self._check_not_pipelined(method, node)
            self._require_synthesizable(method, node)
            assert method is not None
            inputs = self._resolve_call_args(node.value)
            target_names = self._extract_names(node.targets)
            outputs = self._make_output_vars_with_rename(
                target_names, method=method,
            )
            stmt = self._make_call_stmt(method, inputs, outputs)
            for orig_name, v in zip(target_names, outputs):
                v.producer = stmt
                self._scope[orig_name] = v
            return stmt
        raise SynthesisError(
            f"Non-synthesizable assignment at line {node.lineno}"
        )

    def _make_output_vars_with_rename(
        self,
        target_names: list[str],
        method: object,
    ) -> list[HwVar]:
        """Build output ``HwVar``s for an assignment, renaming any local
        that would shadow a kernel parameter declared by the regmap.

        A local named after a regmap field aliases the kernel-signature
        parameter in C++. Without rename, ``y = compute(...)`` followed
        by ``self.regmap.set("y", y)`` lowers to ``ap_int<32> y = ...;
        y = y;`` — a self-assignment that discards the computed value.

        Same-name ``<name> = self.regmap.get("<name>")`` is the existing
        idiom recognized by ``_emit_regmap_get`` (it elides into a
        comment), so we leave that case alone — the parameter is in
        scope, no local is actually declared.
        """
        from waveflow.hw.regmap import RegMapGetStmt
        is_regmap_get = (
            getattr(method, '_stmt_class', None) is RegMapGetStmt
        )
        rm_fields = self._regmap_field_names()
        outputs: list[HwVar] = []
        for n in target_names:
            emit_name = n
            if n in rm_fields and not is_regmap_get:
                emit_name = f"_{n}_local"
            outputs.append(HwVar(name=emit_name, typ=None))
        return outputs

    def _visit_ann_assign(self, node: ast.AnnAssign) -> HwStmt:
        # x: T = yield from self.ep.method(...)
        if node.value is None:
            raise SynthesisError(
                f"Bare annotated assignment is not synthesizable (line {node.lineno})"
            )
        if isinstance(node.value, ast.YieldFrom):
            return self._make_call_with_binding(
                node.value.value, [node.target]
            )
        raise SynthesisError(
            f"Non-synthesizable annotated assignment at line {node.lineno}"
        )

    def _visit_expr_stmt(self, node: ast.Expr) -> HwStmt | None:
        val = node.value
        # Docstring or other bare constant — drop silently.
        if isinstance(val, ast.Constant):
            return None
        # yield from self.ep.method(...)
        if isinstance(val, ast.YieldFrom):
            return self._make_call_stmt_from_node(val.value, node)
        # `yield self.timeout(...)` — a @sim_only latency model.  C-sim is untimed,
        # so a bare yield of a @sim_only call carries no hardware meaning and is
        # stripped (mirrors the @sim_only handling for direct-call expr stmts).
        if isinstance(val, ast.Yield):
            if isinstance(val.value, ast.Call):
                method = self._resolve_method(val.value.func)
                if getattr(method, '_is_sim_only', False):
                    return None
            raise SynthesisError(
                f"Non-synthesizable 'yield' at line {node.lineno}; only a 'yield from' of a "
                f"synthesizable call or a 'yield' of a @sim_only call (e.g. self.timeout) is allowed"
            )
        # self.hook(...) — direct call, no yield from
        if isinstance(val, ast.Call):
            method = self._resolve_method(val.func)
            if getattr(method, '_is_sim_only', False):
                return None  # @sim_only — skip during synthesis
            self._check_not_pipelined(method, node)
            self._require_synthesizable(method, node)
            assert method is not None
            inputs = self._resolve_call_args(val)
            return self._make_call_stmt(method, inputs, [])
        raise SynthesisError(
            f"Non-synthesizable expression statement at line {node.lineno}"
        )

    def _visit_return(self, node: ast.Return) -> ReturnStmt:
        if node.value is None:
            return ReturnStmt(value=None)
        val = node.value
        if isinstance(val, ast.Name):
            if val.id not in self._scope:
                raise SynthesisError(
                    f"Return of undefined variable '{val.id}' at line {node.lineno}"
                )
            return ReturnStmt(value=Ref(var=self._scope[val.id]))
        if (isinstance(val, ast.Attribute)
                and isinstance(val.value, ast.Name)
                and val.value.id in self._scope):
            return ReturnStmt(
                value=FieldRef(var=self._scope[val.value.id], field=val.attr)
            )
        raise SynthesisError(
            f"Non-synthesizable return value at line {node.lineno}"
        )

    def _visit_if(self, node: ast.If) -> CaseStmt:
        # Allowed: 'if var.field <op> value:' or 'if var <op> value:'
        test = node.test
        if not isinstance(test, ast.Compare):
            raise SynthesisError(
                f"Non-synthesizable 'if' condition at line {node.lineno}; "
                f"only 'if var.field == value:' or 'if var <op> value:' is allowed"
            )
        if (len(test.ops) != 1
                or not isinstance(test.ops[0], (ast.Eq, ast.NotEq))):
            raise SynthesisError(
                f"Non-synthesizable 'if' condition at line {node.lineno}; "
                f"only '==' and '!=' comparisons are allowed"
            )
        left = test.left
        field_name: str | None
        if isinstance(left, ast.Attribute) and isinstance(left.value, ast.Name):
            var_name = left.value.id
            field_name = left.attr
        elif isinstance(left, ast.Name):
            var_name = left.id
            field_name = None
        else:
            raise SynthesisError(
                f"Non-synthesizable 'if' condition at line {node.lineno}; "
                f"only 'if var.field <op> value:' or 'if var <op> value:' is allowed"
            )
        if var_name not in self._scope:
            raise SynthesisError(
                f"Undefined variable '{var_name}' in 'if' at line {node.lineno}"
            )
        hw_var = self._scope[var_name]
        cmp_val = self._resolve_compare_rhs(test.comparators[0])
        op = '==' if isinstance(test.ops[0], ast.Eq) else '!='
        body_stmts = self._visit_stmts(node.body)
        else_stmts = self._visit_stmts(node.orelse) if node.orelse else None
        return CaseStmt(
            var=hw_var,
            field=field_name,
            value=cmp_val,
            if_true=SeqStmt(stmts=body_stmts),
            if_false=SeqStmt(stmts=else_stmts) if else_stmts is not None else None,
            op=op,
        )

    # ------------------------------------------------------------------
    # Call-building helpers
    # ------------------------------------------------------------------

    def _make_call_with_binding(
        self,
        call_node: ast.expr,
        targets: list[ast.expr],
    ) -> HwStmt:
        if not isinstance(call_node, ast.Call):
            raise SynthesisError("'yield from' must be followed by a call expression")
        method = self._resolve_method(call_node.func)
        self._check_not_pipelined(method, call_node)
        self._require_synthesizable(method, call_node)
        assert method is not None
        inputs = self._resolve_call_args(call_node)
        kwargs = self._resolve_call_kwargs(call_node)
        outputs = self._make_output_vars(targets)
        stmt = self._make_call_stmt(method, inputs, outputs, kwargs)
        for v in outputs:
            v.producer = stmt
            self._scope[v.name] = v
        return stmt

    def _make_call_stmt_from_node(
        self, call_node: ast.expr, parent: ast.stmt
    ) -> HwStmt:
        if not isinstance(call_node, ast.Call):
            raise SynthesisError(
                f"'yield from' must be followed by a call expression "
                f"(line {getattr(parent, 'lineno', '?')})"
            )
        method = self._resolve_method(call_node.func)
        self._check_not_pipelined(method, parent)
        self._require_synthesizable(method, parent)
        assert method is not None
        inputs = self._resolve_call_args(call_node)
        kwargs = self._resolve_call_kwargs(call_node)
        return self._make_call_stmt(method, inputs, [], kwargs)

    def _make_call_stmt(
        self,
        method: object,
        inputs: list,
        outputs: list[HwVar],
        kwargs: dict | None = None,
    ) -> HwStmt:
        kwargs = kwargs or {}
        stmt_class = getattr(method, '_stmt_class', None)
        if stmt_class is not None:
            return stmt_class(method=method, inputs=inputs, outputs=outputs, kwargs=kwargs)
        synth_fn = getattr(method, '_synth_fn', None)
        if synth_fn is not None:
            return SynthCallStmt(method=method, inputs=inputs, outputs=outputs, kwargs=kwargs)
        impl_file = getattr(method, '_impl_file', None)
        return FunctionStmt(
            method=method, inputs=inputs, outputs=outputs, kwargs=kwargs,
            impl_file=impl_file,
        )

    def _resolve_call_kwargs(self, call_node: ast.Call) -> dict:
        """Resolve keyword args (by name) the same way as positional args.

        Codegen-relevant keywords (e.g. ``max_count`` on m_axi array ops) are
        captured so a statement can project them by name."""
        result: dict = {}
        for kw in call_node.keywords:
            if kw.arg is None:   # **kwargs splat — not synthesizable
                continue
            arg = kw.value
            if isinstance(arg, ast.Name) and arg.id in self._scope:
                result[kw.arg] = self._scope[arg.id]
            elif isinstance(arg, ast.Attribute):
                obj = self._resolve_obj(arg)
                result[kw.arg] = obj if obj is not None else arg
            else:
                result[kw.arg] = arg
        return result

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _resolve_method(self, func_node: ast.expr) -> object | None:
        """Resolve ``self.ep.method`` to the actual bound method."""
        if not isinstance(func_node, ast.Attribute):
            return None
        obj = self._resolve_obj(func_node.value)
        if obj is None:
            return None
        return getattr(obj, func_node.attr, None)

    def _resolve_obj(self, node: ast.expr) -> object | None:
        """Resolve ``self`` or attribute chains rooted at ``self``."""
        if isinstance(node, ast.Name) and node.id == 'self':
            return self._comp
        if isinstance(node, ast.Attribute):
            parent = self._resolve_obj(node.value)
            if parent is None:
                return None
            return getattr(parent, node.attr, None)
        return None

    def _resolve_call_args(self, call_node: ast.Call) -> list:
        result: list = []
        for arg in call_node.args:
            if isinstance(arg, ast.Name) and arg.id in self._scope:
                result.append(self._scope[arg.id])
            elif isinstance(arg, ast.Attribute):
                obj = self._resolve_obj(arg)
                result.append(obj if obj is not None else arg)
            elif isinstance(arg, ast.Call):
                cond = self._try_lower_poll_cond(arg)
                if cond is not None:
                    result.append(cond)
                    continue
                hw_var = self._try_inline_regmap_get(arg)
                result.append(hw_var if hw_var is not None else arg)
            else:
                result.append(arg)
        return result

    def _try_lower_poll_cond(self, call_node: ast.Call):
        """Recognize an ``Eq(x)`` / ``Ne(x)`` poll-condition constructor used as a
        call-site argument (the ``cond`` of ``poll_until``) and lower it to a
        :class:`~waveflow.hw.memif.LoweredPollCond`.

        The op (``==`` / ``!=``) comes from the class; the rhs resolves via
        :meth:`_resolve_compare_rhs` — a runtime variable already in scope (a
        ``HwVar``) or a compile-time constant.  Returns ``None`` for any other
        call so the caller falls through to its other handlers."""
        from waveflow.hw.memif import Eq, LoweredPollCond, Ne
        func = call_node.func
        cls = self._globals.get(func.id) if isinstance(func, ast.Name) else None
        if cls is Eq:
            op = '=='
        elif cls is Ne:
            op = '!='
        else:
            return None
        if len(call_node.args) != 1 or call_node.keywords:
            raise SynthesisError(
                f"poll_until condition {func.id}(...) takes exactly one positional "
                f"argument (a literal or a runtime value), at line {call_node.lineno}"
            )
        rhs = self._resolve_compare_rhs(call_node.args[0])
        return LoweredPollCond(op=op, rhs=rhs)

    def _try_inline_regmap_get(self, call_node: ast.Call) -> HwVar | None:
        """Recognize ``self.regmap.get("<name>")`` as a sub-expression and
        lower it to a ``HwVar`` named after the field.

        The kernel signature already declares a parameter named after each
        non-vitis-auto regmap field, so emitting ``<name>`` as the call
        argument resolves to the right C++ scalar (or array) directly —
        no temp local, no AST repr leak.
        """
        from waveflow.hw.regmap import RegMapGetStmt
        method = self._resolve_method(call_node.func)
        if method is None:
            return None
        if getattr(method, '_stmt_class', None) is not RegMapGetStmt:
            return None
        if len(call_node.args) != 1 or call_node.keywords:
            return None
        name_node = call_node.args[0]
        if not (isinstance(name_node, ast.Constant)
                and isinstance(name_node.value, str)):
            return None
        field_name = name_node.value
        regmap = getattr(self._comp, 'regmap', None)
        if regmap is None or field_name not in regmap._fields:
            return None
        return HwVar(name=field_name, typ=regmap._fields[field_name].schema)

    def _regmap_field_names(self) -> set[str]:
        """Return the set of regmap field names declared on the component,
        or an empty set when the component has no regmap."""
        regmap = getattr(self._comp, 'regmap', None)
        if regmap is None:
            return set()
        return set(regmap._fields.keys())

    def _make_output_vars(self, targets: list[ast.expr]) -> list[HwVar]:
        names = self._extract_names(targets)
        return [HwVar(name=n, typ=None) for n in names]

    @staticmethod
    def _extract_names(targets: list[ast.expr]) -> list[str]:
        names: list[str] = []
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
        return names

    def _resolve_compare_rhs(self, node: ast.expr) -> object:
        """Resolve the rhs of a synthesizable ``==`` / ``!=`` comparison.

        The condition-IR rhs may be EITHER a compile-time constant (an enum
        member or literal) OR a **runtime variable already in scope** (a
        ``HwVar``).  The latter is what the ring-poll dequeue needs — it compares
        ``tail != head`` where ``head`` is a runtime-read local, not a literal.
        A bare name in scope binds to its ``HwVar``; everything else falls
        through to constant evaluation (which still rejects unsupported forms).
        """
        if isinstance(node, ast.Name) and node.id in self._scope:
            return self._scope[node.id]
        return self._eval_const(node)

    def _eval_const(self, node: ast.expr) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Attribute):
            obj = self._resolve_obj(node.value)
            if obj is not None:
                return getattr(obj, node.attr, node.attr)
            # Module-level reference (e.g. DemoCmdType.END) — preserve the AST
            # node so the resolver can turn it into the real Python value.
            return node
        if isinstance(node, ast.Name):
            return node
        raise SynthesisError(
            f"Cannot evaluate constant expression: {ast.dump(node)}"
        )

    @staticmethod
    def _require_synthesizable(method: object | None, node: ast.AST) -> None:
        if method is None or not getattr(method, '_is_synthesizable', False):
            lineno = getattr(node, 'lineno', '?')
            name = (
                getattr(method, '__name__', repr(method))
                if method is not None
                else '<unresolved>'
            )
            raise SynthesisError(
                f"Call to non-synthesizable method '{name}' at line {lineno}. "
                f"Mark it @synthesizable or @sim_only."
            )

    @staticmethod
    def _check_not_pipelined(method: object | None, node: ast.AST) -> None:
        if method is None:
            return
        method_name = getattr(method, '__name__', None)
        if method_name in _PIPELINED_OP_NAMES:
            lineno = getattr(node, 'lineno', '?')
            raise SynthesisError(
                f"Pipelined stream operation '{method_name}' at line {lineno} of "
                f"the extracted body. Pipelined ops are only legal inside "
                f"@synthesizable hook bodies (their C++ lowering requires "
                f"hand-written pipelined loops with #pragma HLS PIPELINE). "
                f"Refactor to call a hook that takes the stream as an argument "
                f"and does the pipelined I/O internally."
            )


# ---------------------------------------------------------------------------
# Module-level kernel-body selection policy
# ---------------------------------------------------------------------------


def _validate_leaf_is_flat(comp) -> None:
    """A leaf lowers to ONE kernel function, so it must not own a sub-graph.

    The *structural* half of the component contract (``plans/codegen_check_family.md``
    Stage 4); the body half is the rest of the extractor.  A leaf becomes a single C++
    function, and a single function has nowhere to put a sub-component or an internal
    channel between two of them.

    Without this the failure is **silent, not loud**: the extractor walks only the entry
    method, so a ``HostActivated`` carrying a child emitted a perfectly well-formed
    kernel that *never mentioned the child* — and ``check()`` returned ``(True, None)``
    about it.  Dropping half a design without a word is exactly what this family exists
    to stop.

    Lives here rather than in :mod:`waveflow.build.codegen_check` on purpose: rules live
    in the extractor so ``generate`` enforces them and ``check`` relays them, and the two
    cannot drift.  See that module's docstring.
    """
    kids = sorted(getattr(comp, 'sub_comps', None) or {})
    internal = sorted(getattr(comp, 'interfaces', None) or {})
    if not (kids or internal):
        return
    owned = []
    if kids:
        owned.append(f"sub-components {kids}")
    if internal:
        owned.append(f"internal interfaces {internal}")
    raise SynthesisError(
        f"{type(comp).__name__} lowers to a single kernel function, but it owns "
        f"{' and '.join(owned)}. A single function has nowhere to put them, so emitting "
        f"one would silently drop them. Either inline that behaviour into this "
        f"component's own body, or make it a composite — a FreeRunMod with sub-components "
        f"instead of a run_iter body, whose codegen IS the sub-component graph (one task per "
        f"child, one channel per internal edge). See docs/guide/flows/."
    )


def extract_kernel(comp) -> HwStmt:
    """Extract the kernel body and return a fully-resolved ``HwStmt`` tree.

    The path is chosen by :func:`~waveflow.build.codegen_dispatch.codegen_path`
    (typed dispatch on the component's class): a ``testbench`` routes to
    :func:`extract_testbench` (``main``); a ``leaf`` extracts its entry method
    (``HostActivated`` → ``on_start``, ``FreeRunMod`` → ``run_iter``, else the
    regmap fallback ``on_start``/``run_proc``).  The returned tree has every
    ``ast.*`` node replaced with the real Python value and every output
    ``HwVar`` typed where possible.

    A ``composite`` component has no body of its own — its codegen is the graph
    (:func:`composite_top_spec`), so extracting one here is a usage error.

    A ``leaf`` must also be *structurally* a leaf: see :func:`_validate_leaf_is_flat`.
    """
    from waveflow.build.codegen_dispatch import codegen_path
    path = codegen_path(comp)
    if path.kind == 'testbench':
        return extract_testbench(comp)
    if path.kind == 'composite':
        raise SynthesisError(
            f"{type(comp).__name__} is a composite: it has no kernel body to extract; "
            f"its codegen is the sub-component graph (composite_top_spec)."
        )
    _validate_leaf_is_flat(comp)
    from waveflow.build.hwresolve import resolve_kernel  # local: avoid an import cycle
    tree = HwStmtExtractor(comp, method_name=path.method).extract()
    return resolve_kernel(tree, comp)


def extract_testbench(comp) -> HwStmt:
    """Extract the testbench ``main()`` body and return a resolved ``HwStmt`` tree.

    Companion to :func:`extract_kernel`, but for the testbench-source
    pathway introduced by Phase 14: the component is a ``HwTestbench``
    subclass whose ``main()`` method is the codegen root.  Rule profile
    differs from the kernel side — blocking stream ops, file I/O, and
    DUT construction are legal, while pipelined ops stay forbidden.
    """
    from waveflow.build.hwresolve import resolve_testbench
    extractor = HwStmtExtractor(comp, method_name='main', is_testbench=True)
    tree = extractor.extract()
    return resolve_testbench(tree, comp)
