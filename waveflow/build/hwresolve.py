"""Resolve raw AST nodes in an ``HwStmt`` tree to real Python values.

``HwStmtExtractor`` produces a tree whose ``inputs`` lists and
``CaseStmt.value`` fields may still contain ``ast.*`` nodes (names,
constants, attribute chains).  ``resolve_kernel`` walks the tree, looks
up module-level names, converts ``ast.Attribute`` references rooted at a
bound ``HwVar`` into ``FieldRef``, and leaves no ``ast.*`` nodes behind.
"""
from __future__ import annotations

import ast
import sys
from typing import TYPE_CHECKING

from waveflow.hw.hwstmt import (
    CaseStmt,
    DutBindStmt,
    FieldRef,
    HwStmt,
    HwVar,
    KernelCallStmt,
    MemAllocArrayStmt,
    MemBindStmt,
    MemReadArrayStmt,
    ReturnStmt,
    SchemaBindStmt,
    SeqStmt,
    SynthCallStmt,
    TbCallStmt,
    TbFileIOStmt,
    TbRegmapFileReadStmt,
    TbStatusJsonStmt,
    TbStreamIOStmt,
    WhileStmt,
)

if TYPE_CHECKING:
    from waveflow.hw.hw_module import HwModule


class ResolutionError(Exception):
    """Raised when an AST node in the IR cannot be resolved to a value."""


def resolve_kernel(tree: HwStmt, comp: HwModule) -> HwStmt:
    """Walk *tree* and replace AST nodes in inputs/values with real values.

    Mutates *tree* in place. Also returns it for chaining.
    """
    scope: dict[str, HwVar] = {}
    method = _kernel_method(comp)
    module = sys.modules.get(method.__module__)
    globs = getattr(method, '__globals__', {})
    _walk(tree, scope, comp, module, globs)
    return tree


def resolve_testbench(tree: HwStmt, comp) -> HwStmt:
    """Resolve a testbench-extractor tree (entry point ``main``).

    For v1 the resolution rules are a superset of the kernel ones: every
    name + attribute chain that ``resolve_kernel`` already handles is
    handled the same way.  Future phases extend the resolver to also
    resolve DUT-attribute chains (``dut.s_in`` → bound endpoint object)
    once the extractor produces those bindings.
    """
    scope: dict[str, HwVar] = {}
    method = getattr(comp, 'main')
    module = sys.modules.get(method.__module__)
    globs = getattr(method, '__globals__', {})
    _walk(tree, scope, comp, module, globs)
    return tree


def _kernel_method(comp):
    """The bound method codegen resolves against (its ``__module__``/``__globals__``
    set the resolution scope).  The entry is chosen by the same typed dispatch the
    extractor uses (:func:`~waveflow.build.codegen_dispatch.codegen_path`), so
    extraction and resolution never disagree."""
    from waveflow.build.codegen_dispatch import codegen_path
    return getattr(comp, codegen_path(comp).method)


def _walk(stmt, scope, comp, module, globs):
    """Recurse over the tree, resolving as we go and tracking HwVar scope."""
    if isinstance(stmt, WhileStmt):
        _walk(stmt.body, scope, comp, module, globs)
        return
    if isinstance(stmt, SeqStmt):
        for child in stmt.stmts:
            _walk(child, scope, comp, module, globs)
        return
    if isinstance(stmt, CaseStmt):
        stmt.value = _resolve_value(stmt.value, scope, comp, module, globs)
        _walk(stmt.if_true, scope, comp, module, globs)
        if stmt.if_false is not None:
            _walk(stmt.if_false, scope, comp, module, globs)
        return
    if isinstance(stmt, ReturnStmt):
        return
    if isinstance(stmt, (
        DutBindStmt, KernelCallStmt, SchemaBindStmt,
        TbFileIOStmt, TbStreamIOStmt, TbRegmapFileReadStmt, TbStatusJsonStmt,
        MemBindStmt, MemAllocArrayStmt, MemReadArrayStmt,
    )):
        # TB-mode IR nodes carry plain Python data (classes, ast subtrees
        # for path/count exprs, literal field names) — no name resolution
        # against the extractor's HwVar scope is needed.  The emitter
        # walks AST sub-trees directly via _emit_str_expr / _emit_int_expr.
        return
    if isinstance(stmt, TbCallStmt):
        # Reserved IR carrier (not produced by the current extractor).
        for v in stmt.outputs:
            scope[v.name] = v
        return
    if isinstance(stmt, SynthCallStmt):
        stmt.inputs = [
            _resolve_input(x, scope, comp, module, globs) for x in stmt.inputs
        ]
        _populate_output_types(stmt, comp)
        for v in stmt.outputs:
            scope[v.name] = v
        return


def _populate_output_types(stmt, comp):
    """Set ``HwVar.typ`` on each output, per per-statement-type rules.

    Statements whose return type can't be determined locally leave ``typ``
    as ``None`` and are refined by later phases.
    """
    from waveflow.hw.hwstmt import FunctionStmt
    from waveflow.hw.interface import StreamGetStmt
    from waveflow.hw.regmap import RegMapGetStmt

    if isinstance(stmt, StreamGetStmt) and stmt.outputs:
        stmt.outputs[0].typ = stmt.inputs[0]
        return
    if isinstance(stmt, RegMapGetStmt) and stmt.outputs:
        field_name = stmt.inputs[0]
        stmt.outputs[0].typ = comp.regmap._fields[field_name].schema
        return
    if isinstance(stmt, FunctionStmt) and stmt.outputs:
        ret = _unwrap_return_type(stmt.method)
        if ret is None:
            return
        import typing
        if typing.get_origin(ret) is tuple and len(stmt.outputs) > 1:
            for var, t in zip(stmt.outputs, typing.get_args(ret)):
                var.typ = t
        else:
            stmt.outputs[0].typ = ret
        return
    # All other statement types: leave typ=None on any outputs.


def _unwrap_return_type(method):
    """Return the method's declared return type, unwrapping ``ProcessGen[T]``.

    ``ProcessGen`` is a ``Generator[Event, Any, T]`` alias, so we look for
    a ``Generator`` origin and return its third type argument.
    """
    import collections.abc
    import typing
    try:
        hints = typing.get_type_hints(method)
    except Exception:
        return None
    ret = hints.get('return')
    if ret is None:
        return None
    if typing.get_origin(ret) is collections.abc.Generator:
        args = typing.get_args(ret)
        if len(args) == 3:
            return args[2]
    return ret


def _resolve_input(x, scope, comp, module, globs):
    """Resolve one element of an inputs list.

    Already-resolved values (``HwVar``, primitives, classes, endpoints) pass
    through. ``ast`` nodes are converted to their referenced Python values.
    """
    if isinstance(x, HwVar):
        return x
    if isinstance(x, ast.Constant):
        return x.value
    if isinstance(x, ast.Name):
        if x.id in scope:
            return scope[x.id]
        return _lookup_name(x.id, module, globs, x)
    if isinstance(x, ast.Attribute):
        root = _attribute_root(x)
        if isinstance(root, ast.Name) and root.id in scope:
            # var.field — convert to FieldRef on the bound var.
            # Multi-level chains (var.field.subfield) are out of scope.
            if not isinstance(x.value, ast.Name):
                raise ResolutionError(
                    f"Nested attribute chain {ast.dump(x)} not supported"
                )
            return FieldRef(var=scope[root.id], field=x.attr)
        return _resolve_attr_chain(x, module, globs)
    return x


def _resolve_value(v, scope, comp, module, globs):
    """Resolve a ``CaseStmt.value`` (literal, enum member, etc.)."""
    if isinstance(v, ast.AST):
        return _resolve_input(v, scope, comp, module, globs)
    return v


def _attribute_root(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node


def _lookup_name(name, module, globs, ast_node):
    if name in globs:
        return globs[name]
    if module is not None and hasattr(module, name):
        return getattr(module, name)
    raise ResolutionError(
        f"Cannot resolve name '{name}' at line {getattr(ast_node, 'lineno', '?')}"
    )


def _resolve_attr_chain(node, module, globs):
    """Resolve ``Module.X`` / ``Module.X.Y`` as a Python value."""
    if isinstance(node, ast.Name):
        return _lookup_name(node.id, module, globs, node)
    if isinstance(node, ast.Attribute):
        parent = _resolve_attr_chain(node.value, module, globs)
        return getattr(parent, node.attr)
    raise ResolutionError(f"Unsupported expression {ast.dump(node)}")
