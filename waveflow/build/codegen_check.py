"""codegen_check.py — ``check(source, target)``: *would this lower?*, as a predicate.

Codegen is **one dispatch, two modes** over ``(source × target)``::

    generate(source, target)  =  validate(source, target) + emit(...)
    check(source, target)     =  validate(source, target)          -> (ok, err_msg)

Before this module there was no extractability *predicate*: every rule lived inside the extractor and
**raised** (20+ ``SynthesisError`` sites in :mod:`waveflow.build.hwcodegen` — implicit ``self.X``
capture, non-``@synthesizable`` calls, forbidden statement shapes, bad yields).  The only way to ask
*"would this synthesize?"* was to try to synthesize it and catch fire.

**How this cannot drift.**  ``check`` does not know a single rule.  It runs the **real extraction**
(:func:`~waveflow.build.hwcodegen.extract_kernel` / :func:`~waveflow.build.hwcodegen.extract_testbench`),
throws the tree away, and converts a raise into a verdict.  A second copy of the rules — a mirror, a
re-implementation, a "lightweight" syntactic pre-pass — would be a **shadow**: it would answer for
rules that no longer exist and miss rules that do.  Running the same code is the whole design.  Do not
add a rule here; add it to the extractor, and ``check`` reports it for free.

**``check`` is user/test/docs-facing.**  The codegen path keeps raising (it is already fail-loud, and
a build wants the traceback).  Nothing should call ``check`` and then ``generate`` — that extracts
twice to learn the same thing.

See ``plans/codegen_check_family.md``; the target vocabulary is
:mod:`waveflow.hw.codegen_targets` (shared with ``docs/guide/flows/index.md``).
"""
from __future__ import annotations

from waveflow.hw.codegen_targets import ALL_TARGETS, IMPLEMENTED_TARGETS


def _source_class(source) -> type:
    """Resolve *source* (a class **or** an instance) to its class."""
    return source if isinstance(source, type) else type(source)


def potential_targets(source) -> frozenset[str]:
    """The codegen targets that **exist for** *source*'s kind.

    Reads the ``potential_targets`` ClassVar declared by each kind
    (:class:`~waveflow.hw.hw_hostactivated.HostActivated`,
    :class:`~waveflow.hw.hw_freerun.FreeRunComp`, :class:`~waveflow.hw.hw_composite.CompositeComp`,
    :class:`~waveflow.hw.hw_testbench.SeqTB`) via ``getattr`` — house style, the same way ``build/``
    reads ``cpp_kernel_name`` / ``control_mode`` / ``_is_testbench``.  A source that declares none
    (e.g. a plain :class:`~waveflow.hw.hw_component.HwComponent` not yet on an execution-model class)
    has an empty set: no target is claimed to exist for it.

    *Potential*, not *supported*: this states the paths that exist for the **kind**.  Whether this
    particular component actually makes it down one is :func:`check`'s answer, not the class's.

    Accepts a class or an instance.
    """
    return frozenset(getattr(_source_class(source), "potential_targets", frozenset()))


def _no_targets_message(cls: type) -> str:
    """Explain an empty ``potential_targets`` — without sending the caller round in a circle.

    Naming a target explicitly cannot help here (gate 2 rejects every name against an empty set), so
    this must not say "name one explicitly".  For the case that actually occurs — a plain
    ``HwComponent`` that predates the execution-model classes — the honest answer names the migration
    *and* admits that codegen still emits for it, so a caller who has watched ``generate`` succeed is
    not told something they can see is false.
    """
    from waveflow.hw.hw_component import HwComponent

    if isinstance(cls, type) and issubclass(cls, HwComponent):
        return (
            f"{cls.__name__} is a plain HwComponent: it has not been migrated to an execution-model "
            f"class (HostActivated / FreeRunComp / CompositeComp), so no codegen target is declared "
            f"for its kind and check() cannot answer for it. Note this is NOT a claim that it will "
            f"not generate — codegen still emits for un-migrated leaves through the interim fallback "
            f"in codegen_dispatch (extracting on_start when a regmap is present, else run_proc). "
            f"Migrating it onto an execution-model class is what makes it checkable."
        )
    return (
        f"{cls.__name__} is not a codegen source: it declares no potential targets and is not a "
        f"HwComponent or SeqTB. Known targets: {_sorted(ALL_TARGETS)}."
    )


def _resolve_target(source, cls: type) -> tuple[str | None, str | None]:
    """Pick the implied target when the caller named none.

    Unambiguous only when the kind has exactly one potential target — which is the common case for a
    DUT (the targets are ~1:1 with the class).  Zero or several means the caller has to say.
    """
    targets = potential_targets(source)
    if len(targets) == 1:
        return next(iter(targets)), None
    if not targets:
        return None, _no_targets_message(cls)
    return None, (
        f"{cls.__name__} has several potential targets ({_sorted(targets)}), so the target cannot "
        f"be inferred; name one explicitly."
    )


def _sorted(names) -> str:
    return "{" + ", ".join(repr(n) for n in sorted(names)) + "}"


def check(source, target: str | None = None) -> tuple[bool, str | None]:
    """Would *source* lower to *target*?  Returns ``(True, None)`` or ``(False, message)``.

    *source* is a component/testbench **class or instance** — not a bare function.  Resolving
    ``self.X`` against the allow-list needs an *elaborated* component (only the syntactic subset is
    checkable from a function alone), so a class is elaborated internally and the call site still
    reads ``check(SimpFunComponent)``.

    *target* names the lowering (see :mod:`waveflow.hw.codegen_targets`).  ``None`` means *the
    source's only potential target*, and is an error when there is not exactly one.

    The gates, in order — the first failure returns:

    1. **Unknown target** — not a name the vocabulary knows (a typo).
    2. **Not a potential target for the kind** — the name is real, but this kind has no such path
       (``check(SomeHostActivated, "concurrent_systemc_tb")``).  This is the question the target axis
       exists to answer.
    3. **Declared but not implemented** — the path exists for the kind but codegen cannot produce it
       yet (Flow 2/3/4 future work).
    4. **The rules** — run the real extraction and convert its raise into a verdict.

    The ``(ok, msg)`` shape reports the **first** violation.  Collecting every violation in one pass
    is a later refinement; the return is shaped so it can grow into a structured report without
    breaking callers.
    """
    cls = _source_class(source)

    # Gate 1 — is this even a target?
    if target is not None and target not in ALL_TARGETS:
        return False, (
            f"Unknown codegen target {target!r}. Known targets: {_sorted(ALL_TARGETS)}."
        )

    if target is None:
        target, msg = _resolve_target(source, cls)
        if target is None:
            return False, msg

    # Gate 2 — does this path exist for this KIND?  (The class states the kind.)
    kind_targets = potential_targets(source)
    if not kind_targets:
        # Empty is not "you picked the wrong one of several" — no name would work. Say the real thing.
        return False, _no_targets_message(cls)
    if target not in kind_targets:
        return False, (
            f"{target!r} is not a potential target for {cls.__name__}; its potential targets are "
            f"{_sorted(kind_targets)}"
        )

    # Gate 3 — the path exists for the kind, but codegen cannot walk it yet.
    if target not in IMPLEMENTED_TARGETS:
        return False, (
            f"{target!r} is a potential target for {cls.__name__}, but it is not implemented yet: "
            f"codegen cannot emit {target!r} for any source today (implemented targets are "
            f"{_sorted(IMPLEMENTED_TARGETS)}). See docs/guide/flows/ for the flow that will build it."
        )

    # Gate 4 — the rules.  Run the REAL extraction; the rules live there and nowhere else.
    return _check_extracts(source, cls)


def _check_extracts(source, cls: type) -> tuple[bool, str | None]:
    """Run the real extraction for *source* and turn a ``SynthesisError`` into a verdict.

    Deliberately calls :func:`~waveflow.build.hwcodegen.extract_testbench` /
    :func:`~waveflow.build.hwcodegen.extract_kernel` and **discards the tree**: we want the rules
    executed, not the output.  Every rule is inside them, so this cannot report a rule codegen does
    not enforce, nor miss one it does.
    """
    # Local imports: hwgen/hwcodegen pull in the whole emitter, and this module is imported for a
    # predicate.  (Matches the local-import style the rest of build/ uses to keep load order loose.)
    from waveflow.build.elaborate import elaborate
    from waveflow.build.hwcodegen import (
        SynthesisError,
        extract_kernel,
        extract_testbench,
    )

    comp = elaborate(cls) if isinstance(source, type) else source

    try:
        if getattr(cls, "_is_testbench", False):
            extract_testbench(comp)
        else:
            extract_kernel(comp)
    except SynthesisError as e:
        # SynthesisError ONLY. It is the extractor's "this is not in the synthesizable subset"
        # verdict — the one exception that is a legitimate answer to the question `check` asks.
        # Anything else (TypeError, AttributeError, ParamPurityError, ...) is a BUG in the source
        # or in codegen, not a validation result; swallowing it would report "not synthesizable"
        # for a broken elaboration and hide the traceback that explains it. Let it propagate.
        return False, str(e)
    return True, None
