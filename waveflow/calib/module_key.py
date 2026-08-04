"""module_key.py — the content-addressed identity of a calibratable module.

Every timing or resource fact we measure is a fact about **one module in one configuration**, and it
has to be filed somewhere a *different* design can find it again.  This module supplies that address.

The address is the module's **structure**, not its parameter dict::

    module_key(FirCompute, {"ntap": 32, "samp_w": 16, "unroll_lane": False})
    -> 'fir_compute-1f0a3c7d'

Keying on :func:`~waveflow.build.elaborate.structure_signature` rather than on ``params`` is what makes
the DSE projection *mechanical*.  The premise of per-module modelling is that a system of ``N`` modules
draws its ``P`` parameters from overlapping subsets — module *i* is a function of only some of ``p``.
Nobody declares those subsets by hand: elaborate the top, walk :func:`walk_modules`, and each leaf's
signature already reflects exactly the parameters that reached it.  Two different system-level ``p``
that induce the same module configuration therefore produce the *same key* and reuse the same
synthesis.  That reuse is most of the saving DSE is after, and it costs nothing to obtain.

It also disposes of a modelling trap for free.  ``unroll_lane`` is not a regression feature — flipping
it is a *different circuit*, with a different multiplier count and a different loop structure.  A
params-tuple key would tempt a fit *across* it; a structure key gives it a separate model by
construction.

Three properties this file is responsible for, all enforced rather than hoped for:

* **Purity** — the key comes from :func:`~waveflow.build.elaborate.elaborate`, whose param-purity gate
  already asserts that structure is a function of the parameters alone.  A module whose structure
  depends on identity or a global counter has no well-defined key, and says so.
* **Stability across processes** — the digest is SHA-256 over a canonical serialization, never
  :func:`hash` (randomized per process for strings).  A signature that embeds a CPython object address
  is rejected by :class:`UnstableSignatureError` instead of silently producing a key that changes on
  the next run.  A calibration library addressed by an unstable key is worse than no library: it
  never hits, and every miss looks like new work rather than a bug.
* **Boundness** — a module whose ports are not yet wired has an *undetermined* structure (an unbound
  stream endpoint has ``queue_size=None`` until :meth:`Interface.bind` supplies the channel depth, and
  FIFO depth is physical).  Keying one produces an address no real composite ever emits, so
  :class:`UnboundModuleError` fronts it.  See :func:`assert_bound`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from waveflow.build.elaborate import elaborate, structure_signature

#: Hex characters of the digest carried in the short key.  8 keeps keys readable while making an
#: accidental prefix collision ~1e-4 across a thousand-module library; the *full* digest is stored in
#: the identity record regardless, so the store can detect a collision rather than merge two modules.
KEY_DIGEST_CHARS = 8

#: The CPython default ``<Foo object at 0x7f...>`` repr.  Its presence in a serialized signature means
#: the signature embeds a memory address, so the key would differ on every run.
_ADDRESS_REPR = re.compile(r"<[^>]* at 0x[0-9a-fA-F]+>")

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class UnboundModuleError(RuntimeError):
    """A module was keyed before its ports were bound, so its structure is not yet fully determined.

    A stream endpoint's ``queue_size`` is ``None`` until :meth:`Interface.bind` hands it the channel
    depth.  That makes an unbound module unusable three ways at once, all of which this check fronts:
    FIFO depth is *physical* (it costs resources and shapes backpressure), codegen already refuses it
    (``depth=None`` -> ``SynthesisError``), and pysim would give the endpoint unbounded capacity — the
    exact condition under which backpressure silently disappears.

    The consequence for calibration specifically is a **join failure**: a standalone fixture that keys
    an unbound module files under an address no composite ever produces, so its records are dead on
    arrival and every lookup looks like a cache miss rather than a bug.  Bind the module's ports in the
    harness — which a synthesizable harness must do anyway — before keying it.
    """


class UnstableSignatureError(RuntimeError):
    """A module's structure signature is not reproducible across processes.

    Raised when the canonical serialization embeds a CPython object address, which happens when
    structure reaches an object with no ``__dict__`` whose ``repr`` is the default one.  The fix is on
    the module: give that object a ``__dict__``-bearing or value-based representation so its identity
    stops leaking into the structure.  Failing here is deliberate — the alternative is a key that
    changes every run, so the calibration library silently never hits.
    """


def snake_name(cls_name: str) -> str:
    """``FirCompute`` -> ``fir_compute`` — the human-readable half of a key.

    Present only so keys can be eyeballed and grepped in a directory listing; correctness rests
    entirely on the digest.
    """
    return _CAMEL_BOUNDARY.sub("_", cls_name).lower()


def _serialize(sig: Any) -> str:
    """Canonically serialize a structure signature to a stable string.

    :func:`~waveflow.build.elaborate.structure_signature` already returns a canonical tree of
    primitives with every unordered collection sorted, so ``repr`` of it is deterministic *given that
    no leaf is identity-dependent* — which is precisely what :func:`signature_digest` then checks.
    """
    return repr(sig)


def signature_digest(comp: Any) -> str:
    """The full hex SHA-256 of *comp*'s structure signature.

    Raises :class:`UnstableSignatureError` if the serialized signature embeds an object address.
    """
    text = _serialize(structure_signature(comp))
    leak = _ADDRESS_REPR.search(text)
    if leak is not None:
        raise UnstableSignatureError(
            f"{type(comp).__name__}: structure signature embeds a CPython object address "
            f"({leak.group(0)!r}), so the module key would change on every run and the calibration "
            f"library would never hit.  Structure reached an object with no __dict__ whose repr is "
            f"the default one; give it a value-based repr (or keep it out of structure) so its "
            f"identity stops leaking."
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolved_params(comp: Any) -> dict[str, Any]:
    """The **resolved** ``HwParam`` values on an elaborated *comp* — declared defaults included.

    The overrides handed to :func:`elaborate` are only the ones a caller bothered to pass; the values
    that actually shaped the hardware are whatever the instance ended up holding.  Recording the
    resolved set is what lets a key be read back as a human-meaningful configuration in a report, and
    what makes two spellings of the same configuration (one explicit, one defaulted) visibly identical.
    """
    from waveflow.hw.hw_module import _hw_param_names

    out: dict[str, Any] = {}
    for name in sorted(_hw_param_names(type(comp))):
        if not hasattr(comp, name):
            continue
        value = getattr(comp, name)
        # HwParamValue is an int subclass; store the plain value so the record is JSON-clean.
        out[name] = int(value) if isinstance(value, int) and not isinstance(value, bool) else value
    return out


@dataclass(frozen=True)
class ModuleIdentity:
    """What a calibration record is *about*: one module class in one resolved configuration.

    Serialized to ``module.json`` beside that module's timing and resource records.  The short
    :attr:`key` is the directory name; :attr:`signature` is the full digest the store compares on load
    so a prefix collision is detected instead of merging two different modules' data.
    """

    key: str
    cls_name: str
    cls_module: str
    params: dict[str, Any] = field(default_factory=dict)
    signature: str = ""

    def to_json(self) -> dict:
        return {"key": self.key, "cls_name": self.cls_name, "cls_module": self.cls_module,
                "params": dict(self.params), "signature": self.signature}

    @classmethod
    def from_json(cls, data: dict) -> "ModuleIdentity":
        return cls(key=data["key"], cls_name=data["cls_name"], cls_module=data["cls_module"],
                   params=dict(data.get("params") or {}), signature=data.get("signature", ""))


def boundary_endpoint_ids(top: Any) -> frozenset:
    """The ``id()`` of every endpoint on *top*'s declared boundary, or empty if it has none.

    A boundary port faces **outside** the design, so its depth is set by whatever encloses the top, not
    by the top — which is why such a port is legitimately unbound within the design and is exempted
    from :func:`assert_bound`.  ``boundary`` is derived (a child endpoint not wired to one of the
    composite's internal interfaces *is* a boundary port), so this needs no declaration to be right.
    """
    try:
        entries = getattr(top, "boundary", ()) or ()
    except Exception:
        # A composite that has not named its boundary raises rather than returning; that is the
        # composite's business, and it simply means nothing is exempt here.
        return frozenset()
    ids = set()
    for entry in entries:
        try:
            ids.add(id(entry[1]))
        except (TypeError, IndexError):
            continue
    return frozenset(ids)


def assert_bound(comp: Any, exempt: "frozenset | set" = frozenset()) -> None:
    """Raise :class:`UnboundModuleError` if any of *comp*'s endpoints has an unresolved depth.

    Checked on every endpoint that *has* a ``queue_size`` (``m_axi`` and register endpoints do not, and
    are irrelevant here), except those whose ``id()`` is in *exempt* — the enclosing design's boundary
    ports (:func:`boundary_endpoint_ids`).  See :class:`UnboundModuleError` for why an unbound
    *internal* port must not be keyed.
    """
    unbound = sorted(
        name for name, ep in (getattr(comp, "endpoints", {}) or {}).items()
        if hasattr(ep, "queue_size") and getattr(ep, "queue_size", None) is None
        and id(ep) not in exempt
    )
    if unbound:
        raise UnboundModuleError(
            f"{type(comp).__name__}: endpoint(s) {unbound} have queue_size=None — the module's ports "
            f"are not bound, so its structure (and therefore its key) is not yet determined.  Wire the "
            f"module into its harness before keying it: a key taken now would file records under an "
            f"address no real composite produces, and every later lookup would miss silently."
        )


def identify_instance(comp: Any, *, require_bound: bool = True,
                      exempt: "frozenset | set" = frozenset()) -> ModuleIdentity:
    """Build a :class:`ModuleIdentity` from an **already elaborated** module.

    The entry point used when walking a system top (:func:`walk_modules`), where the sub-modules were
    constructed by their parent and there is no ``(class, params)`` pair to re-elaborate from.

    *require_bound* enforces :func:`assert_bound`.  Leave it on for anything that will be **stored**;
    turn it off only to inspect a module in isolation (a report, a test), where the resulting key is
    for looking at rather than filing under.  *exempt* passes through the enclosing design's boundary
    endpoints.
    """
    if require_bound:
        assert_bound(comp, exempt)
    digest = signature_digest(comp)
    cls = type(comp)
    return ModuleIdentity(
        key=f"{snake_name(cls.__name__)}-{digest[:KEY_DIGEST_CHARS]}",
        cls_name=cls.__name__,
        cls_module=cls.__module__,
        params=resolved_params(comp),
        signature=digest,
    )


def identify(comp_class: type, params: dict[str, Any] | None = None, *,
             require_bound: bool = False) -> ModuleIdentity:
    """Elaborate *comp_class* with *params* and return its :class:`ModuleIdentity`.

    Goes through :func:`~waveflow.build.elaborate.elaborate`, so the param-purity gate runs: a module
    whose structure is not a function of its parameters raises rather than receiving a key that means
    nothing.

    *require_bound* defaults to **False** here, unlike :func:`identify_instance`: elaborating a leaf
    on its own necessarily leaves its ports unwired, so this entry is inherently the "look at it in
    isolation" one.  A key obtained this way is not the key the same module carries inside a composite
    — bind it in a harness first if the intent is to *store* against it.
    """
    return identify_instance(elaborate(comp_class, params or {}, name="_calib"),
                             require_bound=require_bound)


def module_key(comp_class: type, params: dict[str, Any] | None = None, *,
               require_bound: bool = False) -> str:
    """The short content-addressed key for ``(comp_class, params)`` — e.g. ``fir_compute-1f0a3c7d``."""
    return identify(comp_class, params, require_bound=require_bound).key


def walk_modules(top: Any, *, include_top: bool = True) -> "list[tuple[str, Any, ModuleIdentity]]":
    """Walk an elaborated *top* and return ``[(path, module, identity), ...]`` depth-first.

    This is the projection that makes per-module modelling work without hand-declared parameter
    subsets: the system's ``p`` flows into sub-modules through ordinary construction, so each leaf's
    identity already encodes the subset that reached it.  Feeding those keys to the store is how a
    system-level estimate becomes a sum of per-module lookups.

    *path* is the dotted instance path (``"fir_block.fir_compute"``), which is context — two instances
    at different paths with the same configuration share one key, and that is the point.  Order is
    declaration order (``sub_comps`` is insertion-ordered).

    Boundness is enforced (this is the storage path), with *top*'s own boundary ports exempted: those
    face outside the design, so their depth is the enclosing context's to set, not this design's.
    """
    out: list[tuple[str, Any, ModuleIdentity]] = []
    exempt = boundary_endpoint_ids(top)

    def visit(comp: Any, path: str) -> None:
        out.append((path, comp, identify_instance(comp, exempt=exempt)))
        for name, child in getattr(comp, "sub_comps", {}).items():
            visit(child, f"{path}.{name}")

    if include_top:
        visit(top, getattr(top, "name", type(top).__name__))
    else:
        for name, child in getattr(top, "sub_comps", {}).items():
            visit(child, name)
    return out


def config_id(kernel_task: Any) -> str:
    """The **configuration-qualified** id of a task: its function name plus its template arguments.

    ``mem_r_stream_framed_task`` with ``template_args=(32,)`` -> ``mem_r_stream_framed_task_32``.

    This is the granularity at which a task is actually *synthesized* — it is exactly the prefix Vitis
    gives the RTL entity — and therefore the granularity at which a measurement about it is valid.  The
    bare function name is not: one ``mem_r_stream_framed_task`` directory would serve every memory
    width, so a residual fit at 32 bits would be handed to a design at 64 with nothing to notice.

    Shared deliberately between the resource path (which keys RTL rows by it) and the timing path
    (which keys residuals by it), so the two cannot drift on what counts as "the same configuration".
    """
    args = "".join(f"_{int(a)}" for a in (getattr(kernel_task, "template_args", None) or ()))
    return f"{kernel_task.task_fn}{args}"
