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

from waveflow.hw.codegen_targets import (
    ALL_TARGETS,
    COMPOSITE_KERNEL,
    CONTROL_DRIVEN_KERNEL,
    IMPLEMENTED_TARGETS,
    REALIZATION_HOOKS,
    SEQUENTIAL_VITIS_TB,
    SEQUENTIAL_XSI_TB,
)


def _source_class(source) -> type:
    """Resolve *source* (a class **or** an instance) to its class."""
    return source if isinstance(source, type) else type(source)


def potential_targets(source) -> frozenset[str]:
    """The codegen targets that **exist for** *source*'s kind.

    Reads the ``potential_targets`` ClassVar declared by each kind
    (:class:`~waveflow.hw.hw_hostactivated.HostActivated`,
    :class:`~waveflow.hw.hw_freerun.FreeRunMod` — standalone or composite,
    :class:`~waveflow.hw.hw_testbench.SeqTB`) via ``getattr`` — house style, the same way ``build/``
    reads ``cpp_kernel_name`` / ``control_mode`` / ``_is_testbench``.  A source that declares none
    (e.g. a plain :class:`~waveflow.hw.hw_module.HwModule` not yet on an execution-model class)
    has an empty set: no target is claimed to exist for it.

    *Potential*, not *supported*: this states the paths that exist for the **kind**.  Whether this
    particular component actually makes it down one is :func:`check`'s answer, not the class's.

    Accepts a class or an instance.
    """
    return frozenset(getattr(_source_class(source), "potential_targets", frozenset()))


def _hook_clause(cls: type, target: str | None) -> str:
    """The sentence naming the **realization hook** *target* needs and *cls* does not declare.

    A refusal that names only the *kind* answers a question the caller did not ask.  "``StreamDriver``
    is not on an execution-model class" is true, but what they wanted to know is *why it cannot go
    inside the top* — and the answer is that it declares no ``kernel_task()``: no pre-written body,
    nothing to instantiate.  Naming the peer hook when it IS present turns the refusal into a
    diagnosis: this module is realized on the **other side of the cut** (``plans/design_cut.md``).

    Empty when the target has no hook, or when the class already declares the one it needs (in which
    case the hook is not what is wrong and saying so would mislead).
    """
    from waveflow.hw.hw_module import declares_hook

    hook = REALIZATION_HOOKS.get(target or "")
    if hook is None or declares_hook(cls, hook):
        return ""
    peers = [h for t, h in sorted(REALIZATION_HOOKS.items())
             if h != hook and declares_hook(cls, h)]
    also = (f" It does declare {', '.join(h + '()' for h in peers)} — the realization hook(s) for a "
            f"module realized on the OTHER side of the cut." if peers else "")
    return (f" It also declares no {hook}() hook, which is what a module realized as {target!r} "
            f"provides.{also}")


def _no_targets_message(cls: type, target: str | None = None) -> str:
    """Explain an empty ``potential_targets`` — without sending the caller round in a circle.

    Naming a target explicitly cannot help here (gate 2 rejects every name against an empty set), so
    this must not say "name one explicitly".  For the case that actually occurs — a plain
    ``HwModule`` that predates the execution-model classes — the honest answer names the migration
    *and* admits that codegen still emits for it, so a caller who has watched ``generate`` succeed is
    not told something they can see is false.

    *target*, when the caller named one, adds :func:`_hook_clause`: the kind is only half the answer.
    """
    from waveflow.hw.hw_module import HwModule

    if isinstance(cls, type) and issubclass(cls, HwModule):
        return (
            f"{cls.__name__} is a plain HwModule: it has not been migrated to an execution-model "
            f"class (HostActivated / FreeRunMod), so no codegen target is declared "
            f"for its kind and check() cannot answer for it. Note this is NOT a claim that it will "
            f"not generate — codegen still emits for un-migrated leaves through the interim fallback "
            f"in codegen_dispatch (extracting on_start when a regmap is present, else run_proc). "
            f"Migrating it onto an execution-model class is what makes it checkable."
            + _hook_clause(cls, target)
        )
    return (
        f"{cls.__name__} is not a codegen source: it declares no potential targets and is not a "
        f"HwModule or SeqTB. Known targets: {_sorted(ALL_TARGETS)}."
        + _hook_clause(cls, target)
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
        return None, _no_targets_message(cls, None)
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
    reads ``check(SimpFun)``.

    *target* names the lowering (see :mod:`waveflow.hw.codegen_targets`).  ``None`` means *the
    source's only potential target*, and is an error when there is not exactly one.

    The gates, in order — the first failure returns:

    1. **Unknown target** — not a name the vocabulary knows (a typo).
    2. **Not a potential target for the kind** — the name is real, but this kind has no such path
       (``check(SomeHostActivated, "sequential_xsi_tb")``).  This is the question the target axis
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
        return False, _no_targets_message(cls, target)
    if target not in kind_targets:
        return False, (
            f"{target!r} is not a potential target for {cls.__name__}; its potential targets are "
            f"{_sorted(kind_targets)}" + _hook_clause(cls, target)
        )

    # Gate 3 — the path exists for the kind, but codegen cannot walk it yet.
    if target not in IMPLEMENTED_TARGETS:
        return False, (
            f"{target!r} is a potential target for {cls.__name__}, but it is not implemented yet: "
            f"codegen cannot emit {target!r} for any source today (implemented targets are "
            f"{_sorted(IMPLEMENTED_TARGETS)}). See docs/guide/flows/ for the flow that will build it."
        )

    # Gate 4 — the rules.  Run the REAL generator for THIS target; the rules live there, nowhere else.
    return _check_generates(source, cls, target)


def _check_generates(source, cls: type, target: str) -> tuple[bool, str | None]:
    """Run the real generator **for *target*** and turn its "cannot lower" signal into a verdict.

    Gate 4 must run *the same code* :func:`~waveflow.build.hwcodegen_steps` runs for *target* — a
    second, "lightweight" copy of the rules would answer for rules that no longer exist and miss rules
    that do.  But there is no single generator: a ``(source × target)`` matrix has several, and which
    one applies is a property of the **target**, not the source's class.  So this dispatches on
    *target* and runs that one, discarding its output (we want the rules executed, not the artifact):

    * ``control_driven_kernel``  → :func:`~waveflow.build.hwcodegen.extract_kernel`
    * ``sequential_vitis_tb``    → :func:`~waveflow.build.hwcodegen.extract_testbench`
    * ``composite_kernel``       → :func:`~waveflow.build.composite_gen.composite_top_spec` (the graph
      walk — a leaf is the 1-task case, so the same walk validates both)
    * ``sequential_xsi_tb``      → :func:`~waveflow.build.composite_gen.tb_top_spec` (the TB graph walk)

    **On the verdict exception.**  The two *extractor* paths raise ``SynthesisError`` for "not in the
    synthesizable subset"; the two *graph* paths raise
    :class:`~waveflow.build.hwcodegen.LoweringError` for "this graph will not lower" (a child with no
    realization hook, an endpoint wired to nothing, a boundary that does not match the graph).  Those
    two are the **only** exceptions that are legitimate *answers* to check's question — anything else
    (``AttributeError``/``ParamPurityError``/…) is a **bug**, and letting it propagate keeps the
    traceback that explains it.  ``LoweringError`` is a ``SynthesisError``, so the one ``except``
    below covers both.

    Classifying the graph half was the follow-up flagged here and in
    ``plans/one_component_two_flows.md``, landed as ``plans/design_cut.md`` S0.  It did **not** change
    ``composite_top_spec``'s exception contract under its existing callers: ``LoweringError`` also
    inherits ``ValueError`` and ``TypeError``, so every site that caught either still does.
    """
    # Local imports: the emitter/graph modules are heavy and this module is imported for a predicate.
    from waveflow.build.elaborate import elaborate
    from waveflow.build.hwcodegen import SynthesisError

    comp = elaborate(cls) if isinstance(source, type) else source

    try:
        if target == CONTROL_DRIVEN_KERNEL:
            from waveflow.build.hwcodegen import extract_kernel
            extract_kernel(comp)
        elif target == SEQUENTIAL_VITIS_TB:
            from waveflow.build.hwcodegen import extract_testbench
            extract_testbench(comp)
        elif target == COMPOSITE_KERNEL:
            from waveflow.build.composite_gen import composite_top_spec
            composite_top_spec(comp)
        elif target == SEQUENTIAL_XSI_TB:
            from waveflow.build.composite_gen import tb_top_spec
            tb_top_spec(comp)
        else:
            # Unreachable: gate 3 has already rejected any non-implemented target, and every
            # implemented target is handled above. If this fires, IMPLEMENTED_TARGETS grew a name
            # without a generator wired here.
            raise AssertionError(
                f"check gate 4 has no generator for implemented target {target!r}"
            )
    except SynthesisError as e:
        return False, str(e)
    return True, None
