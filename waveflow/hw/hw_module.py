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

    # `rtl_mods` / `rtl_ifs` are **lazily created**, not dataclass fields, and that is not a style
    # choice — it is the calibration-key trap, avoided.  A module's key is a digest of its structure
    # signature, which walks the instance `__dict__`; adding two always-present empty registries
    # moved the key of EVERY module in the repository, and every stored measurement filed under the
    # old key became unreachable.  (Caught here by two docs numbers going stale, exactly as
    # `StreamIF.dropped` was caught by `tests/calib/test_key_stability.py`.)  Created on first use,
    # they are invisible to a design that has none — which is every design that existed before them.
    # `add_state` sets the same precedent with `_state`.

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
        ``Simulation`` for pysim).

        The ``HwModule`` annotation is **true** as of ``plans/design_cut.md`` S1.  It used to be
        aspirational: a testbench graph added bare ``SimObj`` participants (``StreamDriver``,
        ``StreamSink``, ``MemoryMod``) to a composite, so the declared type was a lie the type checker
        could not see and the endpoint registry those children lacked had to be worked around with
        attribute-name strings."""
        comp.parent = self
        self.sub_comps[comp.name] = comp

    # -- realization hooks -------------------------------------------------
    #
    # A module can be realized in more than one way, and **which realization applies is a property of
    # the build, not of the class** (``plans/design_cut.md``).  The same three-module design can be cut
    # so that mod1 is the DUT and mod2/mod3 are testbench models, or so that all three are inside the
    # DUT — and nothing about mod2 changed, only where the boundary was drawn.
    #
    # So the two realizations are declared by two OPTIONAL, exactly symmetric hooks, and the **cut**
    # decides which one is consulted:
    #
    #   hook             says                                    used when the module is
    #   ---------------  --------------------------------------  ---------------------------
    #   kernel_task()    "my hls::task body is X"                 INSIDE the cut  (in the top)
    #   rtl_module()     "my pre-written Verilog is Z"            INSIDE the design, BESIDE the top
    #   bfm_model()      "my pre-written cycle model is Y"        OUTSIDE the cut (beside it)
    #
    # `kernel_task()` lives on FreeRunMod (only a free-running module HAS a task body, and a generated
    # leaf derives it).  `bfm_model()` and `rtl_module()` live here, on HwModule, because being
    # realized outside the cut — or as RTL beside the kernel — is not a property of the execution
    # model: a memory, a converter and a stream source are all candidates and none of them is a kernel.
    #
    # A module may declare any, all, or none of them.  **None is not an error**: it is a pysim-only
    # node (an RF channel, a golden reference), and that is a FINDING from `check`, not something a
    # class has to declare about itself.

    def bfm_model(self) -> "object":
        """The pre-written XSI **cycle model** this module is realized as when it lies *outside* the
        cut — the peer of :meth:`~waveflow.hw.hw_freerun.FreeRunMod.kernel_task`.

        Returns a :class:`~waveflow.build.composite_gen.BfmModel`: the C++ class name, this module's
        endpoint attribute names in the model's constructor order, and any literal extra ctor args.

        **Declared, never derived.**  Nothing can extract a cycle-exact protocol FSM from Python
        behaviour, and re-deriving a hand-written, verified bus model would not be progress
        (``plans/xsi_tb_codegen.md`` Stage 0).  So a module in that position simply *names* its model,
        exactly as an override of ``kernel_task()`` names a hand-written ``hls::task`` body.

        **The asymmetry to keep honest.**  ``composite_kernel`` is *derived* — gate 4 runs the real
        extractor, so it can answer "no" with a real reason nobody wrote down.  This side is only
        *resolved*: a registry lookup plus port coverage.  It can say "you named a model and its ports
        line up"; it can never say "your Python behaviour is realizable as a BFM".  That gap is closed
        by a byte-identical vector gate and by nothing else — see
        ``docs/guide/custom_hooks/bfm_model.md``.

        The base raises: overriding is the declaration, detected by identity via
        :func:`declares_hook` (the same way ``FreeRunMod._kind`` detects a ``run_iter`` override).
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no bfm_model() hook, so it has no pre-written cycle "
            f"model to place beside the top. A module realized OUTSIDE the cut overrides bfm_model() "
            f"to name one (see docs/guide/custom_hooks/bfm_model.md); a module realized INSIDE the "
            f"cut declares kernel_task() instead."
        )

    def rtl_module(self) -> "object":
        """The pre-written **Verilog module** this module is realized as, instantiated *beside* the
        generated top — the third member of the family, after
        :meth:`~waveflow.hw.hw_freerun.FreeRunMod.kernel_task` and :meth:`bfm_model`.

        Returns a :class:`~waveflow.build.rtl_gen.RtlModule`: the Verilog module name, the
        pre-written source file(s), the endpoint → Verilog-port map, and the instantiation
        parameters.

        **Declared, never derived, and never generated.**  This hook does not turn Python into
        Verilog — that is the anti-goal it shares with the other two hooks (``plans/rtl_module.md``,
        "Not in scope").  A generator would be re-deriving code that has already been verified in
        simulation, and the artifact's whole value is that it *was* verified.

        **Why a module needs this at all.**  Vitis has no shared memory between processes: a local
        array crossing two ``hls::task`` bodies becomes a synchronizing PIPO channel (silently), and
        one ``bram`` port used both ways is a hard dataflow error.  So a buffer shared by two
        concurrent accessors cannot live *inside* a kernel; it lives beside it, and this is how a
        module says so.

        **The conformance obligation.**  Nothing checks that this module's Python behaviour and the
        declared Verilog agree — exactly as nothing checks Python against C++ for :meth:`bfm_model`.
        Same gap, same answer: a byte-identical vector gate.  See
        ``docs/guide/comp_codegen/rtl_module.md``.

        The base raises: overriding is the declaration, detected by identity via
        :func:`declares_hook`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no rtl_module() hook, so it names no pre-written "
            f"Verilog to instantiate beside the top. A module realized as hand-written RTL "
            f"overrides rtl_module() to name one (see docs/guide/comp_codegen/rtl_module.md); a "
            f"module realized as a generated task inside the top declares kernel_task() instead."
        )

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

    #: One model per ``(class, platform)``.  Params are deliberately **absent** from the key: a
    #: model reads structure off the component it is asked about, so one object prices every
    #: configuration of its class.  That is only true because a model never closes over an instance
    #: -- which :meth:`get_rm` being a *classmethod* is what guarantees.
    _RM_CACHE: ClassVar[dict] = {}

    @classmethod
    def get_rm(cls, platform):
        """This class's resource model for *platform*, or ``None`` to take the default lookup.

        **A classmethod on purpose.**  A resource model must not close over a module instance: it is
        handed the component to predict for, and the same object has to price a whole corpus during
        :meth:`~waveflow.calib.resource_model.ResourceModel.fit` and every sibling during
        ``compose``.  Binding one instance would make every fitted row identical.  Having no ``self``
        makes that mistake impossible rather than merely discouraged.

        Everything instance-specific -- parameters, ports, the partitioning of an array -- reaches
        the model through :meth:`resource_structure` on the component it is asked about, at predict
        time.

        Raise here if *platform* is one this class cannot be modelled on; returning a model that
        silently applies another technology's geometry is the worse failure.
        """
        return None

    @classmethod
    def _rm_key(cls, platform):
        return (cls, getattr(platform, "name", None), getattr(platform, "part", None),
                str(getattr(platform, "dir", "")))

    def add_rm_self(self, platform) -> None:
        """Install what :meth:`get_rm` returns, cached per ``(class, platform)``.

        Override only for a module that genuinely needs the *instance* to choose its model -- and
        expect to justify it, since the model it installs still must not close over ``self``.

        The default is a **lookup against the platform's measurement store**, which is the right answer
        far more often than it sounds: a module that does not vary with the parameters being explored
        was measured once, and its area is a fact to be recalled rather than a function to be fitted.
        Measured across the reference sweep, that was three of four modules.

        A key the store has not seen yields zeros **and** reports ``UNCALIBRATED`` — never a silent
        zero.  A module contributing nothing unnoticed would make a design read as *cheaper* than it
        is, which is the one direction an area estimate must not err: it turns "does not fit" into
        "fits".
        """
        key = self._rm_key(platform)
        if key not in HwModule._RM_CACHE:
            HwModule._RM_CACHE[key] = self.get_rm(platform)
        rm = HwModule._RM_CACHE[key]
        if rm is not None:
            self._resource_model = rm
            return

        from waveflow.calib.record_store import ModuleStore
        from waveflow.calib.resource_model import LookupResourceModel

        if platform is None:
            # No platform, and the class declared no model of its own: there is no store to look a
            # measurement up in.  Left uninstalled rather than faked, so `compose` reports the module
            # UNCALIBRATED instead of contributing a silent zero.
            self._resource_model = None
            return
        self._resource_model = LookupResourceModel(
            name=f"{type(self).__name__}:measured",
            store=ModuleStore(platform.dir), platform=platform)

    def resource_structure(self):
        """What this module physically **contains** — multiplier groups, partitioned arrays, and the
        fabric structures (per-lane datapath, crossbars, counters) its body is built from.

        Declared here, beside :meth:`kernel_task`, because it is a fact about the *design* rather
        than about any model of it: the multiplies and the ``ARRAY_PARTITION`` factor are true
        whether or not anyone ever estimates resources.  Keeping it next to the body it describes is
        what stops the two drifting.

        Returns a :class:`~waveflow.calib.vitis_model.DesignStructure`.  Overriding it is all a
        module needs to do to be priced by
        :class:`~waveflow.calib.vitis_model.VitisResourceModel` — the device geometry, the rounding
        and the basis terms all follow from it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not declare resource_structure(); a VitisResourceModel "
            f"cannot derive any counter without it.  Override it to say what the module contains, "
            f"or install a different model kind in add_rm_self().")

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

    def add_rtl_mod(self, mod: "HwModule") -> None:
        """Register *mod* as a sub-module realized as **hand-written RTL beside** the generated top.

        The third ``add_*`` for a hierarchy, and it is a *separate registry from* :meth:`add_comp`
        on purpose — the distinction is what the whole wiring stage rests on:

        =================  ==============================  ==========================================
        registry           what the child becomes           who instantiates it
        =================  ==============================  ==========================================
        ``add_comp``       an ``hls::task`` in the top      Vitis, from the generated C++
        ``add_rtl_mod``    a Verilog instance in the wrapper  the wrapper emitter, from this registry
        =================  ==============================  ==========================================

        Keeping it out of ``sub_comps`` means every existing walk is correct without being told about
        it: ``ordered_subcomps`` still yields exactly the tasks, so the memory is never asked for a
        ``kernel_task()`` it does not have, and ``derive_boundary`` never offers the memory's own
        ports as boundary ports of the kernel — they are wrapper-internal, which is the point.

        **The module is still in the design.**  It is inside the wrapper, so it is inside the
        synthesized scope: it is what a resource estimate must count (``csynth`` of the kernel alone
        reports none of it), and it is invisible to the testbench, which sees only the wrapper's
        AXI-Stream ports.

        *mod* must declare ``rtl_module()`` — a module with no pre-written Verilog has nothing to
        instantiate, and finding that out at wrapper-emit time would be finding it out late.
        """
        from waveflow.hw.hw_module import declares_hook  # local: same module, keeps the name honest

        if not declares_hook(mod, "rtl_module"):
            raise TypeError(
                f"{type(self).__name__}.add_rtl_mod({type(mod).__name__}): that module declares no "
                f"rtl_module() hook, so there is no Verilog to instantiate beside the kernel. A "
                f"module realized as a generated task goes through add_comp instead."
            )
        mod.parent = self
        self.__dict__.setdefault("_rtl_mods", {})[mod.name] = mod

    @property
    def rtl_mods(self) -> dict:
        """Sub-modules realized as **hand-written RTL beside the generated kernel**, by name.

        Empty (and *absent from the instance*) unless :meth:`add_rtl_mod` was called — see the note
        above the method for why that distinction is load-bearing.

        A **composite child's** RTL modules are included, because there is one wrapper per generated
        top and it has to instantiate every memory the flattened design uses.  Names are already
        instance-qualified (``<parent>_mem``), so two reused buffers do not collide.
        """
        return self._collect_rtl("_rtl_mods")

    @property
    def rtl_ifs(self) -> dict:
        """Interfaces joining a kernel task to an :attr:`rtl_mods` module — **wrapper wires**, not
        internal channels.  Populated by :meth:`add_rtl_if`.

        Aggregated over composite children for the same reason :attr:`rtl_mods` is: the wrapper joins
        the flattened kernel's ``bram`` ports to the memories, and a wire this missed would leave a
        port dangling — which ``wrapper_gen`` refuses rather than lets read zeros forever.
        """
        return self._collect_rtl("_rtl_ifs")

    def _collect_rtl(self, key: str) -> dict:
        """*key*'s registry on this module, plus every composite child's, recursively.

        Reads the private ``__dict__`` entries directly rather than the properties, so the recursion
        is over storage and cannot loop back through an aggregating getter.
        """
        out = dict(self.__dict__.get(key) or {})
        for child in self.sub_comps.values():
            # Only a composite HwModule child can carry a registry; a testbench's SimObj children
            # (drivers, sinks) are not modules and have neither attribute.
            if getattr(child, "sub_comps", None) and hasattr(child, "_collect_rtl"):
                out.update(child._collect_rtl(key))
        return out

    def add_rtl_if(self, interface: "Interface") -> None:
        """Register *interface* as a **wrapper wire** — a task's port joined to an RTL module's port.

        The peer of :meth:`add_if`, and separate from it for the reason
        :class:`~waveflow.hw.bram.BramIF` states: an ``add_if`` edge is an *internal channel*, and
        both its endpoints therefore stop being boundary ports.  A wire to a module *outside* the
        kernel is not that.  One end is inside the generated top and one end is outside it, so the
        inside end **must stay a boundary port** — which it does, automatically and with no change to
        :func:`~waveflow.build.composite_gen.derive_boundary`, precisely because this registry is not
        the one that walk reads.

        That is the mechanism ``plans/rtl_module.md`` S2 predicted ("the BRAM endpoint becomes a
        boundary port automatically, plumbed out by machinery that already runs"), one registry over
        from where the plan guessed it would live.  The wrapper emitter reads this registry to know
        which kernel port joins which memory port.
        """
        self.__dict__.setdefault("_rtl_ifs", {})[interface.name] = interface

    def add_if(self, interface: "Interface") -> None:
        """Register *interface* as an internal edge between two sub-components.

        Not for external interface endpoints — those are :meth:`add_endpoint`.
        This records the master↔slave connection so the composite codegen can
        lower it (an on-chip FIFO/BRAM inside a kernel; AXI between IPs in a
        system build), keeping a single introspectable graph on the parent.

        **An interface that SPANS the wrapper seam is swept into both registries.**
        :class:`~waveflow.hw.locked_mem.LockedT2pMemIF` is one object holding four channels, and
        they do not lower the same way: its two ``StreamIF``\\ s are internal edges, while its two
        :class:`~waveflow.hw.bram.BramIF`\\ s are wrapper wires whose kernel-side ends must STAY
        boundary ports.  Such an interface declares the second half through ``rtl_interfaces()``,
        and it is filed here rather than at every call site — a composite that registered the lock
        and forgot the two memory wires would get a dangling ``bram`` port, which
        :func:`~waveflow.build.wrapper_gen.wrapper_spec` refuses, but only after codegen.  The
        default is no such interfaces, so nothing that was already lowering changes.
        """
        self.interfaces[interface.name] = interface
        for rtl_if in getattr(interface, "rtl_interfaces", list)():
            self.add_rtl_if(rtl_if)

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


def _hook_sentinel_base(cls) -> type:
    """The framework class whose definition of a hook **is** the "not declared" sentinel for *cls*.

    Two roots carry realization hooks, and they are peers rather than a hierarchy: a **node** hook
    (``bfm_model``) is defined on :class:`HwModule`, an **edge** hook (``xsi_model``) on
    :class:`~waveflow.hw.interface.Interface`.  Picking the wrong root is not a cosmetic error — it
    is the exact ``hasattr`` trap :func:`declares_hook` exists to close, one level up: comparing an
    ``Interface`` subclass's ``xsi_model`` against ``HwModule``'s (which is ``None``) answers *True*
    for every interface, including the base.

    Imported lazily because ``hw_module`` is imported *by* ``interface``, not the other way round.
    """
    from waveflow.hw.interface import Interface

    return Interface if issubclass(cls, Interface) else HwModule


def declares_hook(source, hook: str, base: type | None = None) -> bool:
    """Does *source* **declare** the realization hook named *hook*, or merely inherit the base's
    "not declared" sentinel?

    ``hasattr`` cannot answer this and must not be used for it.  Once :meth:`HwModule.bfm_model`
    exists as a base method, ``hasattr(mod, "bfm_model")`` is ``True`` for *every* module including
    the DUT — which is exactly the probe the testbench walk used to identify participants.  So the
    predicate is identity against the base, the same way ``FreeRunMod._kind`` detects a ``run_iter``
    override and ``FreeRunMod.kernel_task`` documents detecting its own.

    A hook the base class does not define at all (``kernel_task``, which lives on ``FreeRunMod``) has
    no sentinel to compare against, so *present* is *declared* — correct, because a ``FreeRunMod``
    leaf really does have one (derived from the module itself), and a plain ``HwModule`` really has
    none.

    *base* names the class holding the sentinel.  ``None`` resolves it from *source* via
    :func:`_hook_sentinel_base` — :class:`HwModule` for a module, ``Interface`` for an interface —
    so ``declares_hook(iface, "xsi_model")`` is correct without the caller restating which root it
    means.  Pass it explicitly only when asking about a root the object does not derive from.

    Accepts a class or an instance.
    """
    cls = source if isinstance(source, type) else type(source)
    fn = getattr(cls, hook, None)
    if fn is None:
        return False
    if base is None:
        base = _hook_sentinel_base(cls)
    return fn is not getattr(base, hook, None)
