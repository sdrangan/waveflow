"""synth_report.py — attribute a csynth report's resources to the Waveflow modules that caused them.

A Vitis C-synthesis report already contains a **per-RTL-module breakdown**, which is a nearly-free
first cut at per-module attribution: it needs no standalone synthesis harness, only the whole-top run
the build already does.  That matters for sequencing — it means the additive premise behind the
resource model can be *tested* before the expensive machinery to test it properly is built.

Two things stand between the report and a usable number, and both are traps rather than chores.

**The report is hierarchical.**  ``fir_compute_serial_task_32_s`` reports DSP=32, LUT=3728, and its
child ``fir_compute_serial_task_32_Pipeline_FIR`` reports DSP=32, LUT=2554 — the parent's figure
*already contains* the child's.  Summing every row double-counts enormously (and produces a total far
above the top, which at least fails loudly; a subtler nesting would not).  So only the **task rows** are
summed, and their ``_Pipeline_*`` descendants are recorded as breakdown, never as addends.

**Names are mangled, but derivably so.**  An RTL module is ``<task_fn>_<template args joined by _>``
plus a Vitis suffix — ``mem_w_stream_framed_done_task`` with ``(32, 8)`` becomes
``mem_w_stream_framed_done_task_32_8_s``.  Both halves come from the module's own
:class:`~waveflow.hw.mem_stream.KernelTask`, so the join is derived rather than tabulated.  A module
whose row cannot be found **raises**: silently dropping one makes the per-module sum look better than
it is, which is the one direction of error this whole exercise cannot tolerate.

What is left over after every module is claimed is the **integration term** — the interconnect, the
FIFOs, the DATAFLOW ``entry_proc``, and whatever HLS shared across task boundaries.  It is reported
separately and never folded into a module, because the whole question the resource model asks is
whether design total ≈ Σ modules + integration, and folding would assume the answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.calib.module_key import ModuleIdentity, walk_modules
from waveflow.calib.record_store import RESOURCE_FIELDS, normalize_resources

#: Marks an RTL row as a *sub-block* of the task above it (``..._Pipeline_FIR``).  Its resources are
#: already inside its parent's figure, so it is breakdown, not an addend.
SUBBLOCK_MARKER = "_Pipeline_"


class UnmappedModuleError(RuntimeError):
    """A Waveflow module has no matching row in the synthesis report.

    Fatal by design.  The alternative — skipping it — makes Σ-modules smaller and therefore makes the
    integration term look larger, which reads as "the modules are well modelled and the glue is
    expensive" when the truth is "we lost a module".  Every other error here is recoverable; this one
    corrupts the conclusion.
    """


@dataclass(frozen=True)
class ModuleResources:
    """One Waveflow module's resources, as attributed from the report."""

    key: str                        #: the content-addressed module key
    path: str                       #: dotted instance path within the elaborated top
    cls_name: str
    rtl_module: str                 #: the RTL entity its figures came from
    resources: dict = field(default_factory=dict)
    subblocks: dict = field(default_factory=dict)   #: ``_Pipeline_*`` children, breakdown only

    def to_json(self) -> dict:
        return {"key": self.key, "path": self.path, "cls_name": self.cls_name,
                "rtl_module": self.rtl_module, "resources": dict(self.resources),
                "subblocks": {k: dict(v) for k, v in self.subblocks.items()}}


@dataclass(frozen=True)
class SynthReport:
    """A whole-top synthesis run, attributed.

    ``integration`` is ``top - Σ modules`` per counter: everything the design costs that no module
    accounts for.  It can be negative if HLS shared logic *across* module boundaries, which is
    information rather than an error — and precisely the cross-block surprise the plan's handful of
    whole-top runs exist to catch.
    """

    top_name: str
    top: dict = field(default_factory=dict)
    available: dict = field(default_factory=dict)
    modules: list = field(default_factory=list)          # list[ModuleResources]
    unclaimed: dict = field(default_factory=dict)        # rtl row -> resources (entry_proc, ...)

    @property
    def module_sum(self) -> dict:
        """Σ of the module rows — **not** a design total (see :attr:`integration`)."""
        total: dict = {}
        for m in self.modules:
            for name in RESOURCE_FIELDS:
                if name in m.resources:
                    total[name] = total.get(name, 0) + int(m.resources[name])
        return total

    @property
    def integration(self) -> dict:
        """``top - Σ modules``, per counter — the third additive term, measured."""
        summed = self.module_sum
        return {name: int(self.top.get(name, 0)) - int(summed.get(name, 0))
                for name in sorted(set(self.top) | set(summed))
                if name in RESOURCE_FIELDS}

    def to_json(self) -> dict:
        return {"top_name": self.top_name, "top": dict(self.top),
                "available": dict(self.available),
                "modules": [m.to_json() for m in self.modules],
                "unclaimed": {k: dict(v) for k, v in self.unclaimed.items()},
                "module_sum": self.module_sum, "integration": self.integration}


def rtl_prefix(kernel_task: Any) -> str:
    """The RTL entity-name prefix a :class:`KernelTask` produces.

    ``mem_w_stream_framed_done_task`` with ``template_args=(32, 8)`` -> ``mem_w_stream_framed_done_task_32_8``.
    The Vitis suffix (``_s``, or a numbered variant) is deliberately **not** predicted — it is a tool
    convention we do not control, so matching is by prefix and the suffix is whatever it is.
    """
    args = "".join(f"_{int(a)}" for a in (kernel_task.template_args or ()))
    return f"{kernel_task.task_fn}{args}"


def _classify(prefix: str, rows: dict) -> "tuple[str | None, dict]":
    """Split the rows matching *prefix* into ``(task_row_name, {subblock_name: resources})``."""
    task_row, subblocks = None, {}
    matches = []
    for name in rows:
        if not name.startswith(prefix):
            continue
        if SUBBLOCK_MARKER in name[len(prefix):] or SUBBLOCK_MARKER in name:
            subblocks[name] = rows[name]
        else:
            matches.append(name)
    if len(matches) == 1:
        task_row = matches[0]
    elif len(matches) > 1:
        # Prefer an exact-plus-short-suffix match, so `foo_32` does not swallow `foo_320`.
        matches.sort(key=len)
        task_row = matches[0]
    return task_row, subblocks


def attribute_resources(top_comp: Any, parser: Any, *, top_name: str | None = None) -> SynthReport:
    """Attribute *parser*'s per-module figures to the modules of the elaborated *top_comp*.

    *parser* is a :class:`~waveflow.utils.csynthparse.CsynthParser`; its
    :meth:`~waveflow.utils.csynthparse.CsynthParser.get_total_resources` and
    :meth:`~waveflow.utils.csynthparse.CsynthParser.get_module_resources` are called here, so the
    caller only has to point it at a solution directory.

    Raises :class:`UnmappedModuleError` if any leaf module has no row — see the class docstring for
    why that is fatal rather than skipped.
    """
    parser.get_total_resources()
    parser.get_module_resources()
    rows = {name: normalize_resources(res)
            for name, res in (getattr(parser, "module_info", None) or {}).items()}

    top_name = top_name or getattr(top_comp, "name", type(top_comp).__name__)
    top = normalize_resources(parser.total_resources)
    available = normalize_resources(parser.available_resources)

    claimed: set = set()
    if top_name in rows:
        claimed.add(top_name)          # the top's own row is the design total, not a module

    modules: list[ModuleResources] = []
    missing: list[str] = []
    for path, comp, ident in walk_modules(top_comp):
        try:
            kt = comp.kernel_task()
        except Exception:
            # A composite has no task of its own — it *is* the top row, already accounted for.
            continue
        prefix = rtl_prefix(kt)
        task_row, subblocks = _classify(prefix, rows)
        if task_row is None:
            missing.append(f"{ident.cls_name} ({path}) expected an RTL row starting {prefix!r}")
            continue
        claimed.add(task_row)
        claimed.update(subblocks)
        modules.append(ModuleResources(
            key=ident.key, path=path, cls_name=ident.cls_name, rtl_module=task_row,
            resources=rows[task_row], subblocks=subblocks))

    if missing:
        raise UnmappedModuleError(
            "could not map every module onto the synthesis report:\n  "
            + "\n  ".join(missing)
            + f"\nRTL rows present: {sorted(rows)}.\nDropping a module would shrink the per-module "
              f"sum and inflate the integration term, so this is fatal rather than skipped."
        )

    unclaimed = {name: res for name, res in rows.items() if name not in claimed}
    return SynthReport(top_name=top_name, top=top, available=available,
                       modules=modules, unclaimed=unclaimed)


def report_from_solution(top_comp: Any, sol_path: str | Path, *,
                         top_name: str | None = None) -> SynthReport:
    """:func:`attribute_resources` against the ``csynth.xml`` under a Vitis solution directory."""
    from waveflow.utils.csynthparse import CsynthParser

    return attribute_resources(top_comp, CsynthParser(sol_path=str(sol_path)), top_name=top_name)


def store_report(report: SynthReport, store: Any, identities: "dict[str, ModuleIdentity]", *,
                 source: str = "hls_estimate", part: str = "", period_ns: float = 0.0,
                 tool: str = "", cost_seconds: float = 0.0) -> list:
    """File *report*'s per-module figures into a :class:`~waveflow.calib.record_store.ModuleStore`.

    *identities* maps module key -> :class:`ModuleIdentity` (from
    :func:`~waveflow.calib.module_key.walk_modules`), which is what lets each record carry the
    provenance a later read verifies against.  ``cost_seconds`` is divided evenly across the modules:
    the synthesis was one indivisible run, and pretending to know each module's share of it would be
    inventing data.
    """
    from waveflow.calib.record_store import resource_record

    share = (cost_seconds / len(report.modules)) if report.modules else 0.0
    written = []
    for m in report.modules:
        ident = identities.get(m.key)
        if ident is None:
            continue
        rec = resource_record(ident, m.resources, source=source, part=part, period_ns=period_ns,
                              tool=tool, cost_seconds=share,
                              extra={"rtl_module": m.rtl_module, "attributed_from": report.top_name})
        store.append(rec, identity=ident)
        written.append(rec)
    return written
