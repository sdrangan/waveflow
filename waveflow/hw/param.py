"""Symbolic parameters for declarative schema specialization (Level-2 parameterization).

Deliberately **low-level and dataschema-free** (no import of :mod:`dataschema`) so the
``specialize`` methods in ``dataschema`` can import the defer decorator without a cycle.
The pieces:

- :class:`Param` — a named symbolic value with a default, supporting arithmetic
  (``2 * data_bw``, ``data_bw + 1``, ``//``, :func:`pmax` / :func:`pmin`) that builds
  :class:`Expr` trees; ``.resolve(env)`` evaluates it against a ``{name: value}`` env.
- :class:`Expr` — a symbolic arithmetic node over params/values.
- :class:`LazyField` — a deferred ``specialize`` call ``(fn, args, kwargs)`` whose
  ``.resolve(env)`` resolves the symbolic arguments (recursively, including tuples and
  nested ``LazyField``/``Param``) and invokes ``fn``.
- :func:`defer_if_symbolic` — a shared decorator: when any argument to a wrapped
  ``specialize`` is symbolic (a ``Param`` / ``Expr`` / ``LazyField``, even nested in a
  tuple such as ``max_shape``), it returns a ``LazyField`` instead of computing; otherwise
  it behaves **byte-identically** to the undecorated method (concrete calls are unchanged).
"""
from __future__ import annotations

import functools
import operator
from typing import Any, Callable


class Symbolic:
    """Base for symbolic values: :class:`Param` leaves and :class:`Expr` nodes.

    Arithmetic operators build :class:`Expr` trees (so derived widths like ``2 * data_bw``
    compose); :meth:`resolve` evaluates against a ``{name: value}`` environment."""

    def resolve(self, env: dict[str, Any]) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def __add__(self, other: Any) -> "Expr":
        return Expr(operator.add, self, other)

    def __radd__(self, other: Any) -> "Expr":
        return Expr(operator.add, other, self)

    def __sub__(self, other: Any) -> "Expr":
        return Expr(operator.sub, self, other)

    def __rsub__(self, other: Any) -> "Expr":
        return Expr(operator.sub, other, self)

    def __mul__(self, other: Any) -> "Expr":
        return Expr(operator.mul, self, other)

    def __rmul__(self, other: Any) -> "Expr":
        return Expr(operator.mul, other, self)

    def __floordiv__(self, other: Any) -> "Expr":
        return Expr(operator.floordiv, self, other)

    def __rfloordiv__(self, other: Any) -> "Expr":
        return Expr(operator.floordiv, other, self)


def _resolve(x: Any, env: dict[str, Any]) -> Any:
    """Resolve a value that may be symbolic / lazy / a (nested) tuple or list."""
    if isinstance(x, (Symbolic, LazyField)):
        return x.resolve(env)
    if isinstance(x, tuple):
        return tuple(_resolve(e, env) for e in x)
    if isinstance(x, list):
        return [_resolve(e, env) for e in x]
    return x


class Param(Symbolic):
    """A named symbolic parameter with a default value.

    Names itself from the schema namespace key — via ``__set_name__`` when assigned as a
    class attribute, and (belt-and-suspenders) collected in
    ``ParamSchema.__init_subclass__``."""

    def __init__(self, default: Any) -> None:
        self.default = default
        self.name: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        if self.name is None:
            self.name = name

    def resolve(self, env: dict[str, Any]) -> Any:
        if self.name in env:
            return env[self.name]
        return self.default

    def __repr__(self) -> str:
        return f"Param({self.name!r}, default={self.default!r})"


class Expr(Symbolic):
    """A symbolic arithmetic node: ``op(left, right)`` over params / values."""

    def __init__(self, op: Callable[[Any, Any], Any], left: Any, right: Any) -> None:
        self.op = op
        self.left = left
        self.right = right

    def resolve(self, env: dict[str, Any]) -> Any:
        return self.op(_resolve(self.left, env), _resolve(self.right, env))

    def __repr__(self) -> str:
        return f"Expr({getattr(self.op, '__name__', self.op)}, {self.left!r}, {self.right!r})"


def pmax(a: Any, b: Any) -> Expr:
    """Symbolic ``max`` (e.g. aligning fractional widths)."""
    return Expr(max, a, b)


def pmin(a: Any, b: Any) -> Expr:
    """Symbolic ``min``."""
    return Expr(min, a, b)


class LazyField:
    """A deferred ``specialize`` call: records ``(fn, args, kwargs)``; :meth:`resolve`
    resolves the symbolic arguments (recursively, including nested ``LazyField``s and
    tuples) against ``env`` and invokes ``fn`` to produce the concrete result."""

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict[str, Any]) -> None:
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def resolve(self, env: dict[str, Any]) -> Any:
        args = tuple(_resolve(a, env) for a in self.args)
        kwargs = {k: _resolve(v, env) for k, v in self.kwargs.items()}
        return self.fn(*args, **kwargs)

    def __repr__(self) -> str:
        return f"LazyField({getattr(self.fn, '__name__', self.fn)}, {self.args!r}, {self.kwargs!r})"


def is_symbolic(x: Any) -> bool:
    """Whether ``x`` (or, recursively, any element of a tuple/list) is symbolic or lazy."""
    if isinstance(x, (Symbolic, LazyField)):
        return True
    if isinstance(x, (tuple, list)):
        return any(is_symbolic(e) for e in x)
    return False


def defer_if_symbolic(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a ``specialize`` so it returns a :class:`LazyField` when any argument is
    symbolic, and otherwise calls ``fn`` unchanged (concrete behavior is byte-identical)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # A plain loop, not any() over generators: this runs on every specialize() call, which a
        # pysim makes per transfer.
        for a in args:
            if is_symbolic(a):
                return LazyField(fn, args, kwargs)
        for v in kwargs.values():
            if is_symbolic(v):
                return LazyField(fn, args, kwargs)
        return fn(*args, **kwargs)

    return wrapper


def resolve_elements(elements: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    """Resolve a schema ``elements`` dict: any ``LazyField`` value (or ``schema`` inside a
    ``{schema, description}`` metadata entry) is resolved against ``env``; concrete entries
    pass through unchanged."""
    return {key: _resolve_element_value(value, env) for key, value in elements.items()}


def _resolve_element_value(value: Any, env: dict[str, Any]) -> Any:
    if isinstance(value, LazyField):
        return value.resolve(env)
    if isinstance(value, dict) and "schema" in value:
        resolved = dict(value)
        resolved["schema"] = _resolve_element_value(value["schema"], env)
        return resolved
    return value
