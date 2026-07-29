from __future__ import annotations

import re
import sys
import warnings
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar
import typing

from waveflow.simulation.simobj import SimObj

if TYPE_CHECKING:
    from waveflow.hw.hw_state import HwState
    from waveflow.hw.interface import Interface, InterfaceEndpoint

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


class DynParam(Generic[T]):
    """Marks a field as an **init-time** parameter — a knob on a fixed artifact.

    The third binding site in the ``Param`` family, distinguished from :class:`HwParam` by *when the
    value binds*:

    - ``HwParam`` binds at **build / elaboration**; the value is baked into the artifact, so distinct
      values mean distinct artifacts (``mem_r_stream_32`` vs ``_64``).  The only kind synthesizable
      code can take.
    - ``DynParam`` binds at **init / pre-sim**; the value is set on the instance and, for a generated
      model, emitted as a member assignment — *one* artifact serves all values.

    The axis is binding time, not synthesizable-vs-not: ``DynParam``'s synthesizable cousin is a
    regmap / ``s_axilite`` register (set at runtime over AXI-Lite, one bitstream for all values).  Its
    first use is XSI testbench-model config (``StreamDriver.in_bundle``), where codegen collects the
    ``DynParam`` fields (:func:`discover_dyn_params`) and emits ``<model>.<field> = <value>;``.

    Bound **once at pre_sim and constant for the run** — *not* a per-cycle value.
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


def _dyn_param_names(cls) -> set[str]:
    """Return the set of ``DynParam``-annotated field names on *cls* (mirrors ``_hw_param_names``)."""
    names: set[str] = set()
    for klass in cls.__mro__:
        for n, hint in getattr(klass, '__annotations__', {}).items():
            if isinstance(hint, str):
                mod = sys.modules.get(klass.__module__)
                globs: dict = vars(mod) if mod is not None else {}
                try:
                    hint = eval(hint, globs)  # noqa: S307
                except Exception:
                    continue
            if typing.get_origin(hint) is DynParam:
                names.add(n)
    return names


def discover_dyn_params(obj: Any) -> dict[str, Any]:
    """Return ``{field: value}`` for every ``DynParam`` field of *obj* whose value differs from the
    class default — the init-time knobs a generator emits as ``<model>.<field> = <value>;``.

    A field left at its default (e.g. ``in_bundle == ""``) is omitted, so nothing is emitted for it.
    """
    cls = type(obj)
    out: dict[str, Any] = {}
    for name in _dyn_param_names(cls):
        val = getattr(obj, name, None)
        if val:  # falsy (empty string / empty list / 0 / None) => nothing to emit
            out[name] = val
    return out


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


def discover_state(obj: Any) -> dict[str, "HwState"]:
    """Return ``{name: HwState}`` for every object registered via :meth:`HwModule.add_state`, in
    declaration order.

    Mirrors :func:`discover_dyn_params` (per-object introspection the generator walks).  A module
    that declared no state returns ``{}`` and costs nothing.

    Entries are **re-resolved against the live attribute**: what ``add_state`` declares is "this
    attribute is state", so if the attribute was rebound after the declaration (a subclass swapping
    in differently-specialized storage), the current object wins.  Without this, codegen would emit
    the type of a stale object while pysim used the new one — a silent divergence, not an error.
    """
    registered = getattr(obj, "_state", None) or {}
    out: dict[str, Any] = {}
    for name, entry in registered.items():
        live = getattr(obj, name, None)
        if live is not None and live is not entry:
            live.name = name
            entry = live
        out[name] = entry
    return out


def state_entry_for(comp: Any, obj: Any) -> "HwState | None":
    """Return the registered :class:`~waveflow.hw.hw_state.HwState` that **is** *obj*, or ``None``.

    Identity, not equality: two state arrays can hold equal contents and still be different
    storage.  This is the predicate the extractor's capture rule and the call-site emitter both
    ask — "is this attribute read a declared state reference?"
    """
    for entry in discover_state(comp).values():
        if entry is obj:
            return entry
    return None


@dataclass
class SynthContext:
    """Parameter context passed to every ``synth_fn`` during codegen."""

    component: HwModule
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
    def from_component(cls, comp: HwModule) -> SynthContext:
        import sys
        params: dict[str, str] = {}
        comp_type = type(comp)
        # Walk only the HwModule subclass layers — stop before HwModule
        # itself to avoid evaluating SimObj TYPE_CHECKING annotations.
        for klass in comp_type.__mro__:
            if klass is HwModule:
                break
            if not issubclass(klass, HwModule):
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

    Created automatically by :meth:`HwModule.__post_init__` when wrapping
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


@dataclass
class HwModule(SimObj):
    """Base class for synthesizable hardware components.

    Subclasses annotate synthesis template parameters with ``HwParam[T]``
    and mark compute methods with ``@synthesizable``.

    Carrying ``@synthesizable`` compute (and being pointed at codegen) is a *usage* axis, not a class
    fact: a plain ``HwModule`` with no ``@synthesizable`` methods is a **behavioral**,
    simulation-only model (a data converter, a memory, a channel) that never generates C++.

    Structurally it is a :class:`~waveflow.simulation.simobj.SimObj` with **connectable
    structure**: typed **endpoints** (the ports it talks to the outside world through) and
    optional **sub-components** wired together by internal **interfaces**.  It is the
    *connectable node* in the design graph (``add_endpoint`` / ``add_comp`` / ``add_if``).
    """

    endpoints: dict[str, InterfaceEndpoint] = field(default_factory=dict)
    """Endpoints of the module, indexed by name."""

    sub_comps: dict[str, "HwModule"] = field(default_factory=dict)
    """Sub-components of this module, indexed by name (a hierarchical
    ``HwModule``).  Populated by :meth:`add_comp`; insertion order is the
    codegen order (task-instantiation order in a composite top)."""

    interfaces: dict[str, "Interface"] = field(default_factory=dict)
    """**Internal** interfaces wiring sub-components together, indexed by name.
    Populated by :meth:`add_if`.  This is not for external endpoints (those use
    :meth:`add_endpoint`) — it is the internal graph edges a composite kernel
    lowers to ``hls_thread_local`` FIFOs / BRAM."""

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

    def add_endpoint(self, endpoint: InterfaceEndpoint) -> None:
        endpoint.comp = self
        self.endpoints[endpoint.name] = endpoint

    def add_comp(self, comp: "HwModule") -> None:
        """Register *comp* as a sub-component (insertion order preserved).

        Analogous to :meth:`add_endpoint` for endpoints: it records the child in
        ``self.sub_comps`` so the hierarchy is introspectable off the parent (the
        composite codegen walks this to instantiate one ``hls::task`` per active
        child).  Sub-component names are not globally unique — the same child
        type in two parents shares a name — which is fine here (they are keyed
        per parent, and every SimObj is separately registered with the
        ``Simulation`` for pysim)."""
        comp.parent = self
        self.sub_comps[comp.name] = comp

    # -- resource models ---------------------------------------------------
    def add_rm(self, platform) -> None:
        """Install a resource model on this module and, recursively, on every sub-module.

        Called once on the **top**; the recursion is the point.  The hierarchy is already in
        :attr:`sub_comps` and the parameters are already the ``HwParam`` values, so neither has to be
        restated — an external registry mapping components to models would only be a second copy of
        information the design already carries, and one that rots when a module is renamed.

        *platform* is passed down rather than stored globally because a model is a statement about a
        *technology*: the same module has a different model on a different part (a DSP48E1 is 25x18, a
        DSP48E2 is 27x18), and the platform carries both the calibration library and the
        :attr:`~waveflow.calib.platform.Platform.res_types` vocabulary the model is expressed in.

        Post-order — children first — so an override on a composite can read what its children
        installed.
        """
        for child in self.sub_comps.values():
            child.add_rm(platform)
        self.add_rm_self(platform)

    def add_rm_self(self, platform) -> None:
        """Install *this* module's own model.  Override to supply a prior, a fit, or an interface model.

        The default is a **lookup against the platform's measurement store**, which is the right answer
        far more often than it sounds: a module that does not vary with the parameters being explored
        was measured once, and its area is a fact to be recalled rather than a function to be fitted.
        Measured across the reference sweep, that was three of four modules.

        A key the store has not seen yields zeros **and** reports ``UNCALIBRATED`` — never a silent
        zero.  A module contributing nothing unnoticed would make a design read as *cheaper* than it
        is, which is the one direction an area estimate must not err: it turns "does not fit" into
        "fits".
        """
        from waveflow.calib.record_store import ModuleStore
        from waveflow.calib.resource_model import LookupResourceModel

        self._resource_model = LookupResourceModel(
            name=f"{type(self).__name__}:measured",
            store=ModuleStore(platform.dir), platform=platform)

    @property
    def resource_model(self):
        """The installed model, or ``None`` if :meth:`add_rm` was never called."""
        return getattr(self, "_resource_model", None)

    def add_state(self, state: "HwState") -> None:
        """Declare *state* as **cross-firing state** — storage that persists between firings.

        The third ``add_*`` registry beside :meth:`add_endpoint` and :meth:`add_comp`, and the
        counterpart to the extractor's implicit-capture rule.  That rule forbids reading mutable
        ``self.X`` from a kernel body because it cannot tell a *constant baked into the design*
        from a *register someone must write* — and guessing is wrong either way.  ``add_state``
        does not relax the rule; it makes the author say which::

            self.taps = HwState(TapArray())
            self.add_state(self.taps)          # ...this one is a register file

        Codegen then emits persistent storage (a ``static`` array in the kernel top or the
        generated ``hls::task`` body) rather than an elaboration-time literal, and a read of
        ``self.taps`` at a hook call site lowers to the bare identifier ``taps``.

        *state* must be an :class:`~waveflow.hw.hw_state.HwState` already bound to an attribute of
        ``self`` — the attribute name **is** the C++ identifier, so it is discovered by identity
        rather than passed separately (one name, no drift).  The hardware facts (access mode,
        partitioning, storage binding) live on the ``HwState``, not here.

        Not to be confused with its neighbours: a ``DynParam`` binds once at pre-sim and is constant
        for the run, while state changes every firing; a regmap field is what the *host* writes; and
        a :class:`~waveflow.hw.memory.MemoryMod` is storage across a *bus*.  ``HwState`` is storage
        **inside** the kernel.
        """
        from waveflow.hw.hw_state import HwState as _HwState

        if not isinstance(state, _HwState):
            raise TypeError(
                f"{type(self).__name__}.add_state expects an HwState, got "
                f"{type(state).__name__}. Wrap the storage: "
                f"self.taps = HwState(TapArray()); self.add_state(self.taps)."
            )
        name = None
        for attr, val in vars(self).items():
            if val is state:
                name = attr
                break
        if name is None:
            raise ValueError(
                f"{type(self).__name__}.add_state: the HwState is not bound to an attribute of "
                f"self, so it has no name to emit. Assign it first "
                f"(self.taps = HwState(TapArray()); self.add_state(self.taps))."
            )
        state.name = name
        registry = getattr(self, "_state", None)
        if registry is None:
            registry = {}
            self._state = registry
        # Re-registering a name REPLACES its entry.  What is declared is "this attribute is
        # state", so a subclass that rebinds self.total (different element format, say) and
        # re-declares it is not an error — the registry follows the attribute.  Insertion order
        # is preserved for a name that already exists, so declaration order is stable.
        registry[name] = state

    def add_if(self, interface: "Interface") -> None:
        """Register *interface* as an internal edge between two sub-components.

        Not for external interface endpoints — those are :meth:`add_endpoint`.
        This records the master↔slave connection so the composite codegen can
        lower it (an on-chip FIFO/BRAM inside a kernel; AXI between IPs in a
        system build), keeping a single introspectable graph on the parent."""
        self.interfaces[interface.name] = interface

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
            if klass is HwModule:
                break
            if not issubclass(klass, HwModule):
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
