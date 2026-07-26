"""codegen_dispatch.py — typed, class-based codegen dispatch.

The codegen-side of the realization matrix: *which* codegen path a component takes is decided by its
**class**, over the single shared extraction engine (:class:`~waveflow.build.hwcodegen.HwStmtExtractor`
and :func:`composite_top_spec`). This replaces the generic ``select_kernel_method`` string-resolver —
the dispatch decision now comes from the component's kind, not a ``_kernel_method``-priority lookup.

| Module | kind | Extracts |
|---|---|---|
| :class:`~waveflow.hw.hw_testbench.SeqTB` (``_is_testbench``) | ``testbench`` | ``main`` |
| :class:`~waveflow.hw.hw_freerun.FreeRunMod` **with sub-components** | ``composite`` | — (the graph → ``composite_top_spec``) |
| :class:`~waveflow.hw.hw_freerun.FreeRunMod` **with a body** | ``leaf`` | ``run_iter`` |
| :class:`~waveflow.hw.hw_hostactivated.HostActivated` | ``leaf`` | ``on_start`` |
| plain :class:`~waveflow.hw.hw_module.HwModule` + regmap | ``leaf`` | ``on_start`` (interim) |
| plain free-running ``HwModule`` | ``leaf`` | ``run_proc`` |

Note the ``FreeRunMod`` rows: standalone vs composite is decided by **content** (does it have
sub-components), not by class — a standalone component is the 1-task degenerate case of a composite,
and both are the *same* class (see ``plans/one_component_two_flows.md``). There is no separate
composite class to route around; a composite is just a ``FreeRunMod`` that added children.

The dispatch is *thin*: it only names the method (and, later, the target/pragma); it does **not**
re-implement extraction. Layering is preserved — this lives in ``build/`` and imports ``hw/`` classes;
``hw/`` never imports ``build/``. The future payoff is a ``(class × target)`` dispatch — one component
lowering to ``hls::task`` **and** SystemC ``SC_THREAD`` — which a single ``_kernel_method`` string could
not express; :attr:`CodegenPath.kind` is the seam that will grow that axis.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodegenPath:
    """How a component's kind lowers to codegen.

    ``kind`` selects the emitter (``'leaf'`` → extract + kernel emit; ``'composite'`` →
    ``composite_top_spec``; ``'testbench'`` → the ``main()`` driver). ``method`` is the body to
    extract for the ``leaf``/``testbench`` kinds, and ``None`` for a ``composite`` (which has no body
    of its own — its codegen is the sub-component graph).
    """

    kind: str
    method: str | None


def codegen_path(comp) -> CodegenPath:
    """Return the :class:`CodegenPath` for *comp*, dispatched on its class.

    The ``isinstance`` ladder is the dispatch table verbatim; order matters because a
    ``HostActivated``/``FreeRunMod`` is also a ``HwModule``, and a testbench is one too.
    """
    # Imported locally to keep this build/ module import-light and avoid any load-order coupling.
    from waveflow.hw.hw_module import HwModule
    from waveflow.hw.hw_freerun import FreeRunMod
    from waveflow.hw.hw_hostactivated import HostActivated

    cls = type(comp)
    # Testbench — a sequential main() driver. Keyed on the flag (not isinstance) so a mixin-declared
    # testbench that sets `_is_testbench` without inheriting SeqTB still routes here.
    if getattr(cls, '_is_testbench', False):
        return CodegenPath('testbench', 'main')
    # Free-running component — leaf OR composite, decided by CONTENT, not class: a FreeRunMod with
    # sub-components is a composite (codegen is the graph → composite_top_spec, no body to extract); a
    # FreeRunMod with a run_iter body is a leaf. This is the same body-XOR-children decision the sim
    # side makes in FreeRunMod._kind — a leaf is the 1-task degenerate case of a composite, one class.
    if isinstance(comp, FreeRunMod):
        return (CodegenPath('composite', None) if comp.sub_comps
                else CodegenPath('leaf', 'run_iter'))
    # Host-activated leaf — the regmap-launched invocation entry.
    if isinstance(comp, HostActivated):
        return CodegenPath('leaf', 'on_start')
    # Interim un-migrated leaf (a plain HwModule, not yet on an execution-model class): a regmap
    # makes it host-activated (on_start), else it is a free-running run_proc. Preserves the
    # pre-class regmap fallback so migration stays byte-identical.
    if isinstance(comp, HwModule):
        from waveflow.hw.regmap import VitisRegMapMMIFSlave
        for ep in getattr(comp, 'endpoints', {}).values():
            if isinstance(ep, VitisRegMapMMIFSlave):
                return CodegenPath('leaf', 'on_start')
        return CodegenPath('leaf', 'run_proc')
    raise TypeError(
        f"codegen_path: no codegen dispatch for {cls.__name__} (not a HwModule kind)"
    )
