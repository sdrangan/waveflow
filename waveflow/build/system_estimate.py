"""system_estimate.py — a whole system's resources, estimated row by row, beside what was measured.

``plans/system_resources.md`` Stage 5.  :func:`estimate_system` is a library function first: elaborate
nothing new, synthesize nothing -- walk the system's instance inventory
(:meth:`~waveflow.build.system_top.SystemTopSpec.instances`), price every instance from what the
platform's store holds, and group the instances by the pysim object they realize.  Milliseconds and no
toolchain, so a design-space exploration can call it per point; ``python -m
waveflow.build.system_estimate`` is the CLI over it (``--json`` for an agent).

How each instance is priced, best first:

=====================  ============================================================================
instance               priced by
=====================  ============================================================================
kernel                 an in-context ``instance`` record from *this* system's synthesis, else
                       its class's own model (``get_rm``) when it declares one -- a fitted model
                       prices configurations never synthesized; else the store: each module's
                       ``resource`` record plus the kernel top's ``integration`` record (the
                       ``hls_estimate`` csynth attribution, ``kernel_resources``)
leaf / IP              an in-context ``instance`` record from *this* system's synthesis
                       (``system_synth``, context = the top), else the configuration's
                       out-of-context ``resource`` record (``leaf_resources``; the crossbar census)
system integration     the system identity's ``integration`` record (``system_synth``)
=====================  ============================================================================

Anything with no data is a row at ``UNCALIBRATED`` with zero counters **and** what measuring it would
cost -- never a silent zero (commitment 5).  When ``system_synth`` has run, the measured total sits
beside the prediction with the gap per counter: the area counterpart of ``compare``.

**Interfaces get a model hook.**  A non-kernel instance's owner (an interface such as the crossbar) may
declare ``get_rm(platform)`` like a module; when it returns a model, that model prices the instance --
so a fitted crossbar model makes an unsynthesized shape ``INTERPOLATED`` rather than ``UNCALIBRATED``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.resource_model import COUNTERS, add_counters, zero_counters

#: What measuring a row would cost, by how it is measured -- typical seconds on this flow's tools.
#: A record's own ``cost_seconds`` replaces the typical value when one exists.
TYPICAL_COST_SECONDS = {"csynth": 120.0, "synth_rtl_ooc": 90.0, "synth_axi_xbar": 240.0,
                        "system_synth": 600.0}

_MEASURE_WITH = {"kernel": "csynth", "leaf": "synth_rtl_ooc", "ip": "synth_axi_xbar",
                 "system": "system_synth"}


def _level_rank(level: ConfidenceLevel) -> int:
    return level.rank


@dataclass
class InstanceEstimate:
    """One RTL instance's price."""

    inst: str
    module: str
    kind: str
    key: str
    counters: dict
    level: ConfidenceLevel
    source: str = ""           #: the record tier (``hls_estimate`` / ``vivado_synth`` / ...) or model
    how: str = ""              #: ``lookup`` / ``compose`` / ``model`` / ``--``
    context: str = ""          #: ``in <top>`` for an in-context record, ``ooc`` for the library
    measure_with: str = ""
    cost_seconds: float = 0.0  #: what measuring it (again) would cost
    facts: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"inst": self.inst, "module": self.module, "kind": self.kind, "key": self.key,
                "counters": dict(self.counters), "level": self.level.value, "source": self.source,
                "how": self.how, "context": self.context, "measure_with": self.measure_with,
                "cost_seconds": self.cost_seconds, "facts": dict(self.facts)}


@dataclass
class EstimateRow:
    """One pysim object -- a module or an interface -- and the instances that realize it."""

    owner: str
    parts: list = field(default_factory=list)     # [InstanceEstimate]

    @property
    def counters(self) -> dict:
        return add_counters(zero_counters(), *[p.counters for p in self.parts])

    @property
    def level(self) -> ConfidenceLevel:
        return min((p.level for p in self.parts), key=_level_rank)

    @property
    def sources(self) -> str:
        seen = []
        for p in self.parts:
            s = p.source + (f" ({p.context})" if p.context else "")
            if s and s not in seen:
                seen.append(s)
        return " + ".join(seen) or "--"

    def to_json(self) -> dict:
        return {"owner": self.owner, "counters": self.counters, "level": self.level.value,
                "sources": self.sources, "instances": [p.to_json() for p in self.parts]}


@dataclass
class SystemEstimate:
    """A system's composed estimate, row by row, and -- when the system was synthesized -- the
    measured total beside it."""

    top: str
    platform: str
    rows: list = field(default_factory=list)          # [EstimateRow], the integration row last
    measured: dict | None = None
    measured_source: str = ""
    measured_at: str = ""

    @property
    def total(self) -> dict:
        return add_counters(zero_counters(), *[r.counters for r in self.rows])

    @property
    def level(self) -> ConfidenceLevel:
        return min((r.level for r in self.rows), key=_level_rank)

    @property
    def gap(self) -> dict | None:
        """``(predicted - measured) / measured`` per counter, where measured is non-zero."""
        if self.measured is None:
            return None
        t = self.total
        return {c: (t.get(c, 0) - m) / m for c, m in self.measured.items()
                if c in COUNTERS and m}

    def weakest(self) -> list[dict]:
        """The instances at the weakest level, each with what measuring it would cost -- what to spend
        on first."""
        worst = self.level
        return [{"owner": r.owner, "inst": p.inst, "measure_with": p.measure_with,
                 "cost_seconds": p.cost_seconds}
                for r in self.rows for p in r.parts if p.level == worst]

    def to_json(self) -> dict:
        return {"top": self.top, "platform": self.platform, "total": self.total,
                "level": self.level.value, "rows": [r.to_json() for r in self.rows],
                "weakest": self.weakest(), "measured": self.measured,
                "measured_source": self.measured_source, "measured_at": self.measured_at,
                "gap": self.gap}

    def format_table(self, counters=("lut", "ff", "bram", "dsp")) -> str:
        """The report as text: a row per owner, the total, and the measured total with the gap."""
        w = max([len(r.owner) for r in self.rows] + [len("measured total")]) + 2
        head = f"{'':{w}}" + "".join(f"{c.upper():>9}" for c in counters) + "   how            measured by"
        lines = [f"{self.top}   platform {self.platform}", head]
        for r in self.rows:
            lines.append(f"{r.owner:{w}}" + "".join(f"{r.counters.get(c, 0):>9,}" for c in counters)
                         + f"   {r.level.value:<14} {r.sources}")
        weakest = min((r for r in self.rows), key=lambda r: _level_rank(r.level)).owner if self.rows else ""
        lines.append(f"{'total':{w}}" + "".join(f"{self.total.get(c, 0):>9,}" for c in counters)
                     + f"   weakest: {self.level.value} ({weakest})")
        if self.measured is not None:
            gap = self.gap or {}
            lines.append(f"{'measured total':{w}}"
                         + "".join(f"{self.measured.get(c, 0):>9,}" for c in counters)
                         + f"   {self.measured_source}"
                         + (f", {self.measured_at[:10]}" if self.measured_at else "")
                         + "   gap " + ", ".join(f"{gap[c]:+.1%} {c.upper()}"
                                                 for c in counters if c in gap))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def _counters(payload: dict) -> dict:
    return {c: int(payload[c]) for c in COUNTERS if c in payload and payload[c] is not None}


def _in_context(i, store, system_key: str | None):
    """The best ``instance`` record of *i* from a synthesis of **this** system -- the one whose
    identity is *system_key* -- or ``None``.  Matched by the system's identity, not the top's name:
    after any change to the system (a leaf edited, a kernel re-synthesized) the old run's numbers
    describe another design."""
    from waveflow.build.system_synth import INSTANCE_TARGET

    if store is None or i.identity is None or not system_key:
        return None
    # By instance as well as identity: two instances of one configuration (markov's two adaptor
    # fronts) share a key, and in context each has its own number.
    recs = [r for r in store.read(i.identity.key, INSTANCE_TARGET)
            if r.payload.get("system") == system_key and r.payload.get("inst", i.inst) == i.inst]
    return max(recs, key=lambda r: (r.fidelity, recs.index(r))) if recs else None


def _kernel_estimate(i, store, platform, cost: float, top: str, use_in_context: bool,
                     system_key: str | None = None) -> InstanceEstimate:
    """A kernel instance: this system's in-context measurement, else its class's model if it
    declares one, else the hls_estimate records."""
    from waveflow.calib.module_key import walk_modules
    from waveflow.calib.record_store import INTEGRATION_TARGET

    hw = i.hw
    base = dict(inst=i.inst, module=i.module, kind="kernel", key=i.identity.key if i.identity else "",
                measure_with="csynth", cost_seconds=cost)
    rec = _in_context(i, store, system_key) if use_in_context else None
    if rec is not None:
        return InstanceEstimate(counters=_counters(rec.payload), level=ConfidenceLevel.EXACT,
                                source=rec.source, how="lookup", context=f"in {top}", **base)
    get_rm = getattr(type(hw), "get_rm", None)
    model = get_rm(platform) if (get_rm is not None and platform is not None) else None
    if model is not None:
        conf = model.confidence(hw)
        return InstanceEstimate(counters=add_counters(zero_counters(), model.predict(hw)),
                                level=conf.level, source=type(model).__name__, how="model",
                                facts={"summary": conf.summary}, **base)
    if store is None:
        return InstanceEstimate(counters=zero_counters(), level=ConfidenceLevel.UNCALIBRATED,
                                how="--", facts={"summary": "no platform: no store to look in"},
                                **base)
    total, sources, missing = zero_counters(), [], []
    for path, comp, ident in walk_modules(hw):
        expects_own = not getattr(comp, "sub_comps", None)       # a leaf: a task or a taskless top
        rec = store.best(ident.key, "resource")
        if rec is not None:
            total = add_counters(total, _counters(rec.payload))
            sources.append(rec.source)
        elif expects_own:
            missing.append(path)
    integ = store.best(i.identity.key, INTEGRATION_TARGET) if i.identity else None
    if integ is not None:
        total = add_counters(total, _counters(integ.payload))
        sources.append(integ.source)
    else:
        missing.append(f"{i.inst} (integration)")
    if missing:
        return InstanceEstimate(counters=total, level=ConfidenceLevel.UNCALIBRATED, how="compose",
                                source=" + ".join(dict.fromkeys(sources)),
                                facts={"summary": f"no record for {missing}", "missing": missing},
                                **base)
    return InstanceEstimate(counters=total, level=ConfidenceLevel.EXACT, how="compose",
                            source=" + ".join(dict.fromkeys(sources)), **base)


def _rtl_estimate(i, store, platform, top: str, use_in_context: bool,
                  system_key: str | None = None) -> InstanceEstimate:
    """A leaf or IP instance: in-context, else out-of-context, else its owner's model hook."""
    measure = _MEASURE_WITH[i.kind]
    base = dict(inst=i.inst, module=i.module, kind=i.kind, key=i.identity.key if i.identity else "",
                measure_with=measure, cost_seconds=TYPICAL_COST_SECONDS[measure])
    if store is not None and i.identity is not None:
        rec = _in_context(i, store, system_key) if use_in_context else None
        if rec is not None:
            return InstanceEstimate(counters=_counters(rec.payload), level=ConfidenceLevel.EXACT,
                                    source=rec.source, how="lookup", context=f"in {top}",
                                    **{**base, "cost_seconds": rec.cost_seconds or
                                       base["cost_seconds"]})
        rec = store.best(i.identity.key, "resource")
        if rec is not None:
            return InstanceEstimate(counters=_counters(rec.payload), level=ConfidenceLevel.EXACT,
                                    source=rec.source, how="lookup", context="ooc",
                                    **{**base, "cost_seconds": rec.cost_seconds or
                                       base["cost_seconds"]})
    owner = i.owner
    get_rm = getattr(type(owner), "get_rm", None) if owner is not None else None
    from waveflow.hw.hw_module import HwModule

    if get_rm is not None and not isinstance(owner, HwModule) and platform is not None:
        model = get_rm(platform)
        if model is not None:
            conf = model.confidence(owner)
            return InstanceEstimate(counters=add_counters(zero_counters(), model.predict(owner)),
                                    level=conf.level, source=type(model).__name__, how="model",
                                    facts={"summary": conf.summary}, **base)
    return InstanceEstimate(counters=zero_counters(), level=ConfidenceLevel.UNCALIBRATED, how="--",
                            facts={"summary": f"no measurement of {i.module} at this configuration; "
                                              f"its cost is missing from the total, not zero"},
                            **base)


def _group(spec) -> dict[str, str]:
    """``inst -> owner label``: each instance under its owner, a multi-view adaptor's views rolled
    up into the adaptor (the adaptor row is the whole device front)."""
    from waveflow.build.system_dag import owner_label

    roll: dict[int, Any] = {}
    for slot in spec.mi:
        if slot.kind == "adaptor":
            for v in slot.views:
                roll[id(v.owner)] = slot.owner
    out = {}
    for i in spec.instances():
        o = roll.get(id(i.owner), i.owner)
        out[i.inst] = owner_label(o)
    return out


def estimate_system(sysm, platform=None, *, top: str | None = None, inside=None,
                    xbar_name: str | None = None, root: str | Path | None = None,
                    use_in_context: bool = True, spec=None) -> SystemEstimate:
    """Estimate the resources of the system object *sysm* on *platform*, row by row.

    *top* / *inside* / *xbar_name* name and cut the system top as ``add_system_steps`` does (or pass
    the *spec* itself).  *root* is the example root holding the kernels' csynth output, which is what
    the system's identity -- and so its integration and measured-total records -- is computed from;
    without it (or before any csynth) those two read ``UNCALIBRATED`` / absent.  *use_in_context*
    ``False`` ignores this system's own synthesis records, giving the purely composed prediction
    (each part from its own measurement) that the measured total is then a check on.
    """
    from waveflow.build.system_dag import rtl_rel, snake
    from waveflow.build.system_synth import INSTANCE_TARGET, system_identity
    from waveflow.build.system_top import render_system_top, system_top_spec
    from waveflow.build.system_xsi import discover
    from waveflow.calib.record_store import INTEGRATION_TARGET, platform_store

    if spec is None:
        top = top or snake(type(sysm).__name__)
        xbar, _host, cut = discover(sysm)
        spec = system_top_spec(xbar, list(inside) if inside is not None else cut, top=top,
                               xbar_name=xbar_name)
    store = platform_store(platform) if platform is not None else None
    groups = _group(spec)

    # The system's identity: what its own synthesis records are filed under (in-context, the
    # integration term, the measured total).  Needs the kernels' csynth output under *root*.
    sys_ident = None
    kdirs = ({m: Path(root) / rtl_rel(m) for m in spec.modules} if root is not None else None)
    if store is not None and kdirs is not None and all(d.is_dir() for d in kdirs.values()):
        sys_ident, _run = system_identity(spec, render_system_top(spec), kdirs, part="",
                                          period_ns=0.0, impl=False)

    csynth_cost = TYPICAL_COST_SECONDS["csynth"]
    rows: dict[str, EstimateRow] = {}
    for i in spec.instances():
        if i.kind == "kernel":
            if root is not None:
                from waveflow.build.resource_steps import synth_seconds_of
                from waveflow.build.system_dag import seconds_rel

                csynth_cost = synth_seconds_of(Path(root) / seconds_rel(i.module)) or csynth_cost
            k = next(k for k in spec.kernels if k.inst == i.inst)
            est = _kernel_estimate(_KernelView(i, k.hw), store, platform, csynth_cost, spec.top,
                                   use_in_context, sys_ident.key if sys_ident else None)
        else:
            est = _rtl_estimate(i, store, platform, spec.top, use_in_context,
                                sys_ident.key if sys_ident else None)
        rows.setdefault(groups[i.inst], EstimateRow(owner=groups[i.inst])).parts.append(est)

    # The system's own term and its measured total, keyed by the system's identity.
    integ_part = InstanceEstimate(inst=spec.top, module=spec.top, kind="system", key="",
                                  counters=zero_counters(), level=ConfidenceLevel.UNCALIBRATED,
                                  how="--", measure_with="system_synth",
                                  cost_seconds=TYPICAL_COST_SECONDS["system_synth"],
                                  facts={"summary": "the top's own logic (inline decoders, tie-offs) "
                                                    "and cross-boundary effects: measured only by "
                                                    "system_synth"})
    measured = measured_source = measured_at = None
    if sys_ident is not None:
        ident = sys_ident
        integ_part.key = ident.key
        if use_in_context:
            rec = store.best(ident.key, INTEGRATION_TARGET)
            if rec is not None:
                integ_part.counters = _counters(rec.payload)
                integ_part.level, integ_part.source, integ_part.how = (ConfidenceLevel.EXACT,
                                                                       rec.source, "lookup")
                integ_part.context = f"in {spec.top}"
                integ_part.facts = {}
        tot = [r for r in store.read(ident.key, INSTANCE_TARGET)
               if r.payload.get("kind") == "system"]
        if tot:
            rec = max(tot, key=lambda r: (r.fidelity, tot.index(r)))
            measured, measured_source, measured_at = (_counters(rec.payload), rec.source,
                                                      rec.measured_at or "")
    out_rows = list(rows.values()) + [EstimateRow(owner="system integration", parts=[integ_part])]
    return SystemEstimate(top=spec.top, platform=getattr(platform, "name", "") or "",
                          rows=out_rows, measured=measured, measured_source=measured_source or "",
                          measured_at=measured_at or "")


@dataclass
class _KernelView:
    """An inventory instance with its kernel's pysim module beside it (``hw``)."""

    inst_: Any
    hw: Any

    def __getattr__(self, name):
        return getattr(self.inst_, name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(ref: str):
    """``package.module:callable`` -> the system object the callable returns."""
    import importlib

    mod, _, attr = ref.partition(":")
    if not attr:
        raise SystemExit(f"name the system as module:callable, got {ref!r}")
    return getattr(importlib.import_module(mod), attr)()


def main(argv: list[str] | None = None) -> int:
    import argparse

    from waveflow.calib.platform import Platform, platform_fallback_path

    ap = argparse.ArgumentParser(description="Estimate a system's resources from the platform's "
                                             "store: a row per pysim object, the measured total "
                                             "beside it when the system was synthesized.")
    ap.add_argument("system", help="the system, as module:callable (e.g. "
                                   "examples.markov.markov_build:system)")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--platforms-root", default="calib/platforms")
    ap.add_argument("--top", default=None, help="the system top's name (default: the class, snake)")
    ap.add_argument("--xbar-name", default=None)
    ap.add_argument("--root", default=None, help="the example root (its csynth output); default: "
                                                 "the system module's directory")
    ap.add_argument("--composed", action="store_true",
                    help="ignore this system's own synthesis: the purely composed prediction")
    ap.add_argument("--json", action="store_true", help="print the estimate as JSON")
    a = ap.parse_args(argv)
    sysm = _load(a.system)
    import importlib

    root = Path(a.root) if a.root else Path(importlib.import_module(
        a.system.partition(":")[0]).__file__).parent
    plat = Platform.resolve(a.platforms_root, a.platform, fallbacks=platform_fallback_path())
    est = estimate_system(sysm, plat, top=a.top, xbar_name=a.xbar_name, root=root,
                          use_in_context=not a.composed)
    print(json.dumps(est.to_json(), indent=2) if a.json else est.format_table())
    return 0


__all__ = ["EstimateRow", "InstanceEstimate", "SystemEstimate", "TYPICAL_COST_SECONDS",
           "estimate_system", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
