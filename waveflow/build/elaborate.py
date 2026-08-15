"""elaborate.py — the elaboration contract.

Codegen depends on a component's *(class, compile-time parameters)*, never on a
specific runtime instance.  :func:`elaborate` is the single entry that builds a
component purely to read its **structure** — endpoints, sub-components,
interfaces, and boundary — HDL-style *elaboration*.

The contract: a component's structure is a **pure function of its
``HwParam``/``HwConst`` parameters**; ``name``, ``sim``, and runtime data are
elaboration context and must not affect it.  When that holds, codegen
``= elaborate(class, param_set) → structure → C++``, one output per param-set,
instance-independent — any real instance with those params matches the
generated C++ by construction.

Precedent — :class:`~waveflow.hw.dataschema.DataSchema` gets this *for free*
(its structure *is* class attributes + classmethods, read with no
instantiation).  :class:`~waveflow.hw.hw_module.HwModule` builds structure
*imperatively* in ``__post_init__``, so this module **enforces by contract**
(:func:`assert_param_pure`) what DataSchema gets by construction.

Before this module the instantiation was a scattered, sim-coupled,
*unenforced* trick: ``cls(name="_codegen", sim=Simulation())`` appeared at three
codegen sites.  They now all route here.
"""
from __future__ import annotations

from typing import Any

import simpy

from waveflow.simulation.simulation import Simulation


class ElabContext(Simulation):
    """A sim-free stand-in for :class:`Simulation`, used only to *elaborate*.

    Codegen never runs the sim — it constructs a component solely to read its
    ports.  This context supplies exactly what construction needs from the
    ``SimObj`` lifecycle: :meth:`add_obj` (inherited) plus a SimPy ``env`` for
    the resources endpoints allocate in their ``__post_init__``.  It is never
    driven by ``run_sim()``.

    The ``env`` is created **lazily**, so a component that declares its
    structure without touching the sim never allocates one — reinforcing that
    structure is independent of the simulation.  ``ElabContext`` *is a*
    ``Simulation`` (subclass), so construction behaves byte-for-byte as it did
    with a plain ``Simulation()``.
    """

    #: Marker so callers/tests can distinguish an elaboration context from a
    #: real, runnable :class:`Simulation`.
    is_elaboration: bool = True

    def __init__(self) -> None:
        # Deliberately does not call ``super().__init__`` — that eagerly builds
        # a ``simpy.Environment``.  We defer it to :attr:`env` so a truly
        # sim-free elaboration allocates nothing.
        self._sim_objs: list = []
        self._env: simpy.Environment | None = None

    @property
    def env(self) -> simpy.Environment:
        if self._env is None:
            self._env = simpy.Environment()
        return self._env

    @env.setter
    def env(self, value: simpy.Environment) -> None:
        self._env = value

    def run_sim(self) -> None:  # pragma: no cover - defensive
        raise RuntimeError(
            "ElabContext is an elaboration-only stand-in for Simulation; it "
            "must not be run.  Build a real Simulation() to run a component."
        )


def _build(comp_class: type, overrides: dict[str, Any], name: str) -> Any:
    """Construct *comp_class* in a fresh :class:`ElabContext` — no purity check.

    The single low-level construction primitive shared by :func:`elaborate`
    and :func:`assert_param_pure` (the latter must build without recursing
    back into the purity gate).

    A ``SimObj``-derived component (``HwModule``) takes ``sim=``; a sim-less
    :class:`~waveflow.hw.hw_testbench.SeqTB` testbench (a plain ``NamedObject``) does not, so the
    ``sim=`` kwarg is passed only when the class actually accepts it.
    """
    from waveflow.simulation.simobj import SimObj
    if isinstance(comp_class, type) and issubclass(comp_class, SimObj):
        return comp_class(name=name, sim=ElabContext(), **overrides)
    return comp_class(name=name, **overrides)


def elaborate(
    comp_class: type,
    params: dict[str, Any] | None = None,
    *,
    name: str = "_codegen",
    check_purity: bool = True,
) -> Any:
    """Elaborate *comp_class* with *params* and return the built structure.

    Constructs the component in a fresh :class:`ElabContext` with the given
    compile-time ``HwParam``/``HwConst`` overrides applied through the normal
    ``__init__`` path (no immutability bypass).  The returned instance is a
    faithful structural stand-in for any real instance with the same params —
    that is the elaboration contract, enforced by :func:`assert_param_pure`.

    The **param-purity gate**: on the first elaboration of a given
    ``(class, param-set)`` this checks that structure is a pure function of the
    params (:func:`assert_param_pure`) and caches the verdict, so codegen is
    keyed by ``(class, param-set)`` — not by instance.  Pass
    ``check_purity=False`` to skip (e.g. when a caller has already verified).

    This is the single instantiation site for codegen: the scattered
    ``cls(name="_codegen", sim=Simulation())`` calls all route here.
    """
    overrides = dict(params or {})
    if check_purity:
        key = _purity_key(comp_class, overrides)
        if key is None or key not in _purity_verified:
            assert_param_pure(comp_class, overrides)
            if key is not None:
                _purity_verified.add(key)
    return _build(comp_class, overrides, name)


# ---------------------------------------------------------------------------
# Param-purity enforcement — the elaboration contract's safety net.
#
# The invariant: a component's STRUCTURE (endpoints, sub-components,
# interfaces, boundary) is a pure function of its compile-time parameters.
# ``assert_param_pure`` elaborates twice — with *different* names, in fresh
# contexts — and asserts the two structures are identical under a
# name/identity-agnostic fingerprint (:func:`structure_signature`).  Any
# difference means structure leaks non-parameter state (identity, a global
# counter, wall-clock time, randomness, or external mutable state), which would
# make ``elaborate(class, params) → C++`` ill-defined.
# ---------------------------------------------------------------------------


class ParamPurityError(RuntimeError):
    """Raised when a component's structure is not a pure function of its params.

    Signals a violation of the elaboration contract: two elaborations with
    identical parameters produced different structure, so codegen keyed by
    ``(class, param-set)`` would be ambiguous.
    """


#: Process-global cache of ``(class, param-set)`` already verified pure, so the
#: gate runs once per unique key rather than on every :func:`elaborate` call.
_purity_verified: set = set()


def _purity_key(comp_class: type, overrides: dict[str, Any]) -> tuple | None:
    """A hashable memo key for a ``(class, param-set)``, or ``None`` if the
    overrides are unhashable (then the gate always runs, never caches)."""
    try:
        key = (comp_class, tuple(sorted(overrides.items())))
        hash(key)  # unhashable override value -> not memoizable
        return key
    except TypeError:
        return None


def assert_param_pure(
    comp_class: type, params: dict[str, Any] | None = None
) -> None:
    """Assert *comp_class*'s structure is a pure function of *params*.

    Elaborates the class twice — with distinct names, in fresh contexts — and
    compares their :func:`structure_signature`.  Raises :class:`ParamPurityError`
    with the first structural difference if they disagree.
    """
    overrides = dict(params or {})
    sig_a = structure_signature(_build(comp_class, overrides, "_elab_a"))
    sig_b = structure_signature(_build(comp_class, overrides, "_elab_b"))
    if sig_a != sig_b:
        diff = _first_diff(sig_a, sig_b) or "(structures differ)"
        raise ParamPurityError(
            f"{comp_class.__name__} structure is not a pure function of its "
            f"parameters {overrides!r}: two elaborations produced different "
            f"structure.  Structure (endpoints / sub-components / interfaces / "
            f"boundary) must depend only on HwParam/HwConst parameters — not on "
            f"identity, global counters, time, randomness, or external mutable "
            f"state.\n  First difference at {diff}"
        )


#: Class-level marker: instances of a type carrying this are elaboration *context*, never structure,
#: wherever they are found and whatever they are called.
#:
#: ``_CONTEXT_ATTRS`` below excludes by attribute **name**, which repairs a known leak but not the
#: mechanism.  That distinction cost 26 orphaned records: ``FirCompute`` held its timing model under a
#: second name the list did not cover, so a ``LinCalibModel`` refactor moved the module's key and every
#: measurement filed against it became unreachable — silently, because a missing key looks exactly like
#: a configuration nobody measured.
#:
#: A calibration model predicts something *about* a module; it is not part of it.  Nothing about a
#: module's ports, sub-components, interfaces or parameters changes because a coefficient was refitted,
#: so a key that moves when a coefficient moves is not addressing structure.  Excluding by type means
#: the next attribute holding one cannot leak, whatever it is named.
_STRUCTURE_CONTEXT_MARKER = "__wf_structure_context__"


def _is_structure_context(value: Any) -> bool:
    """Whether *value* is calibration/runtime context rather than part of the structure."""
    return bool(getattr(type(value), _STRUCTURE_CONTEXT_MARKER, False))


# Object attributes that are elaboration *context* / runtime scaffolding, never
# structure — dropped from the fingerprint so two elaborations compare on
# structure alone (``name``/identity, the sim, simpy resources, and
# back-references are all excluded).
_CONTEXT_ATTRS = frozenset({
    "name", "sim", "parent", "comp", "interface", "_hw_construction_complete",
    "processes", "_process_factories", "action_history", "action_overlaps",
    "track_action_overlaps",
    # Calibration models attached to an instance (``add_rm`` / ``add_timing_model``).  A model is a
    # statement about how this hardware was MEASURED, not about what it is, and attaching one must not
    # change the structure.  This is load-bearing rather than tidy-minded: a module's calibration key
    # is a digest of this signature, so without the exclusion, attaching a model moves the key and
    # every store lookup misses — and ``add_rm`` needs the key to choose the model it is attaching.
    "_resource_model", "_timing_model", "firing_records",
    # Where a model's DATA lives is not part of the design's identity.  A design pointed at two
    # platform directories is one design; without this exclusion it keys as two, so records filed
    # under the first are invisible from the second and every lookup misses **silently** -- the same
    # failure mode as an unbound key.  (plans/harmonize_calib.md, P1.)
    "platform_dir", "calib_dir", "compute_calib_dir",
    # Run-time counters an interface accumulates during a simulation.  What a link CARRIED is not
    # what it IS: a design that dropped a word and one that did not are the same hardware, and the
    # counters are zero until something runs anyway.  Named explicitly because they are plain ints
    # on a structural object, so the type marker above cannot reach them -- and this list is not
    # theoretical here: adding ``StreamIF.dropped`` moved every ``FirBlock`` key in one commit,
    # caught only by ``tests/calib/test_key_stability.py``.
    "dropped", "last_drop_time",                        # StreamIF (offer)
    "blocks_sent", "blocks_delivered", "underrun", "overrun",    # RFSampIF
})

# Attributes holding *name-keyed* structural collections: compare the value
# multiset (the keys are instance names = context) rather than the mapping.
#: ``_rtl_mods`` / ``_rtl_ifs`` belong here for exactly the reason the other three do: they are keyed
#: by instance NAME, and an instance name is elaboration context.  Without them a design with
#: hand-written RTL beside the kernel fails the purity gate on its own instance names — which is the
#: gate working, reporting that the *fingerprint* was reading a name it should not.  (They are spelled
#: with the underscore because the registries are created lazily and live under their private names;
#: a module with no RTL beside it has neither attribute, so its signature — and its calibration key —
#: is exactly what it was.)
_NAME_KEYED_COLLECTIONS = frozenset({"endpoints", "sub_comps", "interfaces",
                                    "_rtl_mods", "_rtl_ifs"})

_numpy_mod: Any = None


def _numpy():
    global _numpy_mod
    if _numpy_mod is None:
        try:
            import numpy as np
            _numpy_mod = np
        except Exception:  # pragma: no cover - numpy is a hard dep here
            _numpy_mod = False
    return _numpy_mod or None


def _is_runtime(value: Any) -> bool:
    """True for a simpy runtime object (per-build identity, not structure)."""
    mod = type(value).__module__ or ""
    return mod == "simpy" or mod.startswith("simpy.")


def _canon(value: Any, seen: frozenset) -> Any:
    """Canonicalize *value* into a hashable, name/identity-agnostic token."""
    if value is None:
        return ("none",)
    if isinstance(value, bool):            # before int: bool is an int subclass
        return ("bool", value)
    if isinstance(value, int):             # incl. HwParamValue -> its int value
        return ("int", int(value))
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, (str, bytes)):
        return ("str", value)
    if isinstance(value, type):
        return ("type", f"{value.__module__}.{value.__qualname__}")
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_canon(v, seen) for v in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_canon(v, seen) for v in value), key=repr)))
    if isinstance(value, dict):
        return ("map", tuple(sorted(
            ((str(k), _canon(v, seen)) for k, v in value.items()),
            key=lambda kv: kv[0])))
    np = _numpy()
    if np is not None:
        if isinstance(value, np.ndarray):
            return ("ndarray", str(value.dtype), tuple(value.shape),
                    tuple(value.ravel().tolist()))
        if isinstance(value, np.generic):
            return ("np", str(value.dtype), value.item())
    if _is_runtime(value):
        # simpy resources are per-build identity only — keep just the type tag.
        return ("runtime", type(value).__name__)
    d = getattr(value, "__dict__", None)
    if d is None:
        return ("opaque", f"{type(value).__module__}.{type(value).__qualname__}",
                repr(value))
    oid = id(value)
    if oid in seen:
        return ("cycle", f"{type(value).__module__}.{type(value).__qualname__}")
    seen = seen | {oid}
    attrs: list = []
    for k in sorted(d):
        if k in _CONTEXT_ATTRS or k.startswith("__"):
            continue
        v = d[k]
        if _is_structure_context(v):
            continue
        if k in _NAME_KEYED_COLLECTIONS and isinstance(v, dict):
            # Name-agnostic: a multiset of canonical values, sorted by content.
            vals = sorted((_canon(x, seen) for x in v.values()), key=repr)
            attrs.append((k, ("multiset", tuple(vals))))
        else:
            attrs.append((k, _canon(v, seen)))
    return ("obj", f"{type(value).__module__}.{type(value).__qualname__}",
            tuple(attrs))


def structure_signature(comp: Any) -> Any:
    """A canonical, name/identity-agnostic structural signature of *comp*.

    Captures the class, endpoints, sub-component graph, interface bindings, and
    any declared ``boundary``/edge lists — excluding object identity, the sim,
    simpy resources, back-references, and instance names.  Two elaborations
    with the same params compare equal iff their **structure** matches.
    """
    return _canon(comp, frozenset())


def _first_diff(a: Any, b: Any, path: str = "<root>") -> str | None:
    """Return a human-readable path to the first structural difference."""
    if a == b:
        return None
    if isinstance(a, tuple) and isinstance(b, tuple) and a and b and a[0] == b[0]:
        tag = a[0]
        if tag == "obj" and len(a) == 3 and len(b) == 3:
            if a[1] != b[1]:
                return f"{path}: class {a[1]} != {b[1]}"
            amap, bmap = dict(a[2]), dict(b[2])
            for k in sorted(set(amap) | set(bmap)):
                if k not in amap:
                    return f"{path}.{k} (present only in 2nd elaboration)"
                if k not in bmap:
                    return f"{path}.{k} (present only in 1st elaboration)"
                sub = _first_diff(amap[k], bmap[k], f"{path}.{k}")
                if sub is not None:
                    return sub
            return path
        if tag in ("seq", "multiset", "set") and len(a) == 2 and len(b) == 2:
            av, bv = a[1], b[1]
            if len(av) != len(bv):
                return f"{path}: {tag} length {len(av)} != {len(bv)}"
            for i, (x, y) in enumerate(zip(av, bv)):
                sub = _first_diff(x, y, f"{path}[{i}]")
                if sub is not None:
                    return sub
        if tag == "map" and len(a) == 2 and len(b) == 2:
            amap, bmap = dict(a[1]), dict(b[1])
            for k in sorted(set(amap) | set(bmap)):
                if k not in amap or k not in bmap:
                    return f"{path}[{k!r}] (present in only one elaboration)"
                sub = _first_diff(amap[k], bmap[k], f"{path}[{k!r}]")
                if sub is not None:
                    return sub
    return f"{path}: {a!r} != {b!r}"
