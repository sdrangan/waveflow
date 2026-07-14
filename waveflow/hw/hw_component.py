from __future__ import annotations

import re
import sys
import warnings
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, ClassVar, Generic, TypeVar
import typing

from waveflow.hw.component import Component

T = TypeVar('T')


class HwParam(Generic[T]):
    """Marks a dataclass field as a C++ template parameter.

    In simulation the field behaves as a normal Python attribute (dataclass
    does not enforce types). At build time the extractor collects all HwParam
    fields from ``get_type_hints()`` and maps them to C++ template names.

    C++ name convention: the Python field name verbatim — ``in_bw`` →
    ``in_bw``.

    Relationship to :class:`~waveflow.hw.param.Param` — one parameter concept,
    two binding sites.  ``HwParam`` is the **component-side, instance-based**
    binding (``comp = MyComp(width=32)`` binds at instantiation; the value drives
    the HLS template params).  The schema ``Param`` is the **type-based** binding
    (``Schema.specialize(width=…)`` produces a type).  They meet at the
    *instance → type bridge*: a component exposes a computed property
    (e.g. ``VmacAccel.Cmd``) that feeds its ``HwParam`` values into a
    ``ParamSchema``'s ``Param`` specialization.  Both build on the single symbolic
    core in :mod:`waveflow.hw.param`; ``HwParam`` keeps this annotation surface so
    existing components are unaffected.
    """


class HwConst(Generic[T]):
    """Marks a class attribute as a class-level constant.

    Translates to ``static constexpr T name = value;`` in generated C++
    (codegen emission added in a follow-up phase). Immutable by convention —
    the framework does not prevent reassignment, but the marker signals
    "do not modify after class definition" to readers and to codegen.

    Usage::

        class CoeffArray(DataArray):
            ncoeff: HwConst[int] = 4
            max_shape = (ncoeff,)
    """


def discover_hw_const(cls) -> dict[str, Any]:
    """Walk the MRO and return ``{field_name: value}`` for every ``HwConst`` field.

    Order is class-MRO declaration order, deduplicated by name (subclass wins).
    Plain fields, ``HwParam`` fields, and ``ClassVar`` literals are excluded.
    """
    result: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        hints = getattr(klass, '__annotations__', {})
        mod = sys.modules.get(klass.__module__)
        globs: dict = vars(mod) if mod is not None else {}
        for name, hint in hints.items():
            if isinstance(hint, str):
                try:
                    hint = eval(hint, globs)  # noqa: S307
                except Exception:
                    continue
            if typing.get_origin(hint) is HwConst:
                if hasattr(klass, name):
                    result[name] = getattr(klass, name)
    return result


_VARIANT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _hw_param_names(comp_class) -> set[str]:
    """Return the set of ``HwParam``-annotated field names on ``comp_class``."""
    names: set[str] = set()
    for klass in comp_class.__mro__:
        for n, hint in getattr(klass, '__annotations__', {}).items():
            if isinstance(hint, str):
                mod = sys.modules.get(klass.__module__)
                globs: dict = vars(mod) if mod is not None else {}
                try:
                    hint = eval(hint, globs)  # noqa: S307
                except Exception:
                    continue
            if typing.get_origin(hint) is HwParam:
                names.add(n)
    return names


def _resolve_variant_values(comp_class, overrides: dict[str, Any]) -> dict[str, Any]:
    """Return the fully-resolved param values for a variant: defaults + overrides."""
    values: dict[str, Any] = {}
    for n in _hw_param_names(comp_class):
        values[n] = getattr(comp_class, n)
    values.update(overrides)
    return values


def validate_param_supports(comp_class) -> None:
    """Validate ``comp_class.param_supports``; raise ``SynthesisError`` on any violation.

    Rules:
    - Must be a ``dict`` (or ``None``).
    - Each key must match ``[A-Za-z_][A-Za-z0-9_]*`` (valid C identifier).
    - Each entry must be a non-empty dict.
    - Each override key must be a declared ``HwParam`` field on the component.
    - Two variants that resolve to the same param configuration emit a
      ``warnings.warn``, but do not raise.
    """
    from waveflow.build.hwcodegen import SynthesisError

    ps = getattr(comp_class, 'param_supports', None)
    if ps is None:
        return
    if not isinstance(ps, dict):
        raise SynthesisError(
            f"{comp_class.__name__}.param_supports must be a dict "
            f"(got {type(ps).__name__})"
        )
    hw_param_names = _hw_param_names(comp_class)
    seen_resolved: dict[tuple, str] = {}
    for key, overrides in ps.items():
        if not isinstance(key, str) or not _VARIANT_KEY_RE.match(key):
            raise SynthesisError(
                f"{comp_class.__name__}.param_supports key {key!r} is not a "
                f"valid C identifier (must match [A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(overrides, dict) or not overrides:
            raise SynthesisError(
                f"{comp_class.__name__}.param_supports[{key!r}] must be a "
                f"non-empty dict of param overrides; got {overrides!r}"
            )
        for name in overrides:
            if name not in hw_param_names:
                raise SynthesisError(
                    f"{comp_class.__name__}.param_supports[{key!r}] overrides "
                    f"unknown parameter {name!r}; declared HwParam fields are "
                    f"{sorted(hw_param_names)}"
                )
        resolved = _resolve_variant_values(comp_class, overrides)
        dup_key = tuple(sorted(resolved.items()))
        if dup_key in seen_resolved:
            warnings.warn(
                f"{comp_class.__name__}.param_supports[{key!r}] resolves to "
                f"the same configuration as {seen_resolved[dup_key]!r}",
                stacklevel=2,
            )
        else:
            seen_resolved[dup_key] = key


class ControlMode(Enum):
    AUTO = auto()           # inferred from HwStmt root at build time
    FREE_RUNNING = auto()   # ap_ctrl_none  (WhileStmt at root)
    PER_INVOCATION = auto() # ap_ctrl_chain (SeqStmt at root)


@dataclass
class SynthContext:
    """Parameter context passed to every ``synth_fn`` during codegen."""

    component: HwComponent
    params: dict[str, str]  # Python name → C++ template param name

    def cpp_param(self, py_name: str) -> str:
        """Return the C++ expression for a parameter.

        Returns the template parameter name (e.g. ``'IN_BW'``) for
        ``HwParam`` fields, or ``repr(value)`` for ``ClassVar`` literals.
        """
        if py_name in self.params:
            return self.params[py_name]
        return repr(getattr(self.component, py_name))

    @classmethod
    def from_component(cls, comp: HwComponent) -> SynthContext:
        import sys
        params: dict[str, str] = {}
        comp_type = type(comp)
        # Walk only the HwComponent subclass layers — stop before HwComponent
        # itself to avoid evaluating SimObj/Component TYPE_CHECKING annotations.
        for klass in comp_type.__mro__:
            if klass is HwComponent:
                break
            if not issubclass(klass, HwComponent):
                break
            raw_ann = vars(klass).get('__annotations__', {})
            mod = sys.modules.get(klass.__module__)
            globs: dict = vars(mod) if mod is not None else {}
            for name, hint_val in raw_ann.items():
                if isinstance(hint_val, str):
                    try:
                        hint = eval(hint_val, globs)  # noqa: S307
                    except Exception:
                        continue
                else:
                    hint = hint_val
                if typing.get_origin(hint) is HwParam:
                    params[name] = name.upper()
        return cls(component=comp, params=params)


class HwParamValue(int):
    """Int subclass that remembers which ``HwParam`` field it was bound to.

    Created automatically by :meth:`HwComponent.__post_init__` when wrapping
    raw values for ``HwParam``-annotated fields. Behaves as a plain ``int``
    for arithmetic, comparison, and protocol checks. Codegen inspects the
    ``.param_name`` attribute to decide between emitting a template name vs
    a literal value.
    """

    param_name: str  # type-only; the runtime attribute is set in __new__

    def __new__(cls, value: int, param_name: str) -> "HwParamValue":
        obj = super().__new__(cls, int(value))
        obj.param_name = param_name
        return obj

    def __repr__(self) -> str:
        return f"HwParamValue({int(self)!r}, {self.param_name!r})"

    def __str__(self) -> str:
        # Format / print / f-string must show the integer value, not the
        # diagnostic repr — codegen f-strings that haven't yet been migrated
        # to ``_stream_template_arg`` rely on this to keep emitting literals.
        # Going through a plain int sidesteps int's __str__/__repr__ slot
        # collision on subclasses that override __repr__.
        return str(int(self))

    def __format__(self, spec: str) -> str:
        # Same reason as __str__: f-string formatting must yield the int.
        return format(int(self), spec)


class HwComponent(Component):
    """Base class for synthesizable hardware components.

    Subclasses annotate synthesis template parameters with ``HwParam[T]``
    and mark compute methods with ``@synthesizable``.
    """

    control_mode: ClassVar[ControlMode] = ControlMode.AUTO
    cpp_kernel_name: ClassVar[str | None] = None
    cpp_namespace: ClassVar[str | None] = None
    """Override for the C++ namespace wrapping hooks for this component.

        None (default): namespace is auto-derived from cpp_kernel_name(cls).
        "":             opt out; hooks emitted in global namespace.
        "<name>":       use this string as the namespace verbatim.

    The kernel function itself is always emitted in the global namespace
    (Vitis HLS requires this).
    """

    param_supports: ClassVar[dict[str, dict[str, Any]] | None] = None
    """Map of variant-suffix-name → param-override-dict.

    Each entry causes the framework to generate an additional concrete kernel
    function named ``<cpp_kernel_name>_<key>`` with the listed ``HwParam``
    overrides applied. Unspecified params use their ``HwParam``-declared
    default.

    A default kernel named ``<cpp_kernel_name>`` (no suffix) is **always**
    generated using ``HwParam`` defaults, regardless of ``param_supports``.

    ``None`` (default) = no additional variants; only the default kernel is
    emitted.
    """

    def __post_init__(self) -> None:
        # Wrap HwParam field values BEFORE super().__post_init__ so any
        # subclass setup that reads self.<param> after super() sees
        # HwParamValue instances.
        self._wrap_hw_params()
        super().__post_init__()
        # Sentinel that flips immutability on. HwParam fields cannot be
        # reassigned once construction has completed.
        object.__setattr__(self, '_hw_construction_complete', True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, '_hw_construction_complete', False):
            for klass in type(self).__mro__:
                klass_hints = getattr(klass, '__annotations__', {})
                if name not in klass_hints:
                    continue
                hint = klass_hints[name]
                if isinstance(hint, str):
                    mod = sys.modules.get(klass.__module__)
                    globs: dict = vars(mod) if mod is not None else {}
                    try:
                        hint = eval(hint, globs)  # noqa: S307
                    except Exception:
                        break
                if typing.get_origin(hint) is HwParam:
                    current = getattr(self, name, None)
                    raise AttributeError(
                        f"Cannot reassign HwParam field '{name}' after "
                        f"construction (current value: {current!r})"
                    )
                break
        object.__setattr__(self, name, value)

    def _wrap_hw_params(self) -> None:
        """Replace each ``HwParam[T]`` field value with a ``HwParamValue`` wrapper."""
        for klass in type(self).__mro__:
            if klass is HwComponent:
                break
            if not issubclass(klass, HwComponent):
                break
            raw_ann = vars(klass).get('__annotations__', {})
            mod = sys.modules.get(klass.__module__)
            globs: dict = vars(mod) if mod is not None else {}
            for name, hint_val in raw_ann.items():
                if isinstance(hint_val, str):
                    try:
                        hint = eval(hint_val, globs)  # noqa: S307
                    except Exception:
                        continue
                else:
                    hint = hint_val
                if typing.get_origin(hint) is not HwParam:
                    continue
                value = getattr(self, name, None)
                if value is None or isinstance(value, HwParamValue):
                    continue
                # object.__setattr__ bypasses the Phase 3 immutability guard.
                object.__setattr__(
                    self, name, HwParamValue(int(value), name)
                )


class SynthComp(HwComponent):
    """Base for components that generate **synthesizable** C++.

    A plain :class:`HwComponent` may be a *behavioral*, simulation-only model of
    hardware Waveflow does not generate (a data converter, a memory, a channel).
    Subclassing ``SynthComp`` declares "this component is synthesizable" and runs
    a construction-time synthesizability check.

    The concrete execution-model subclasses set ``_kernel_method`` to the method
    the codegen lowers as the kernel body:
    :class:`~waveflow.hw.hw_freerun.FreeRunComp` (``run_iter``), a host-activated
    component (``on_start``), :class:`~waveflow.hw.hw_testbench.HwTestbench`
    (``main``). See ``docs/guide/components/taxonomy.md``.
    """

    #: Class-level **intent metadata**: the method this kind lowers as its kernel body — declared by
    #: the execution-model subclass (``FreeRunComp`` → ``run_iter``, ``HostActivated`` → ``on_start``).
    #: ``None`` (the base default) means *no declared entry* — a bare ``SynthComp`` with no execution
    #: model, whose entry is inferred at codegen.
    #:
    #: Construction verifies the declared entry exists (:meth:`_check_synthesizable`); ``None`` skips
    #: that check.  The **codegen dispatch itself is class-based**
    #: (:func:`~waveflow.build.codegen_dispatch.codegen_path`), not a read of this string, so this is
    #: intent, not the mechanism.  (Keeping it ``None`` rather than a concrete ``'run_proc'`` avoids
    #: falsely asserting a bare ``SynthComp`` declares ``run_proc``.)
    _kernel_method: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self._check_synthesizable()

    def _check_synthesizable(self) -> None:
        """Construction-time synthesizability check.

        The base rule: the declared kernel-entry method must actually be
        implemented (not left abstract). Execution-model subclasses extend this
        with their own contract.

        When ``_kernel_method`` is ``None`` (the entry is *inferred* at codegen),
        there is no single declared method to verify here — the inference picks
        ``on_start``/``run_proc``, both of which exist (``on_start`` is
        user-provided on a regmap kernel; ``run_proc`` has a passive default).
        Subclasses that declare a concrete entry (``FreeRunComp``,
        ``HostActivated``) are still checked below.
        """
        method_name = self._kernel_method
        if method_name is None:
            return
        cls = type(self)
        entry = getattr(cls, method_name, None)
        if entry is None or getattr(entry, '__isabstractmethod__', False):
            raise TypeError(
                f"{cls.__name__} is a SynthComp but does not implement its "
                f"kernel-entry method '{method_name}()'."
            )
