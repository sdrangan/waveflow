"""phase_calib.py — calibrating a system's declared timed phases as a framework step
(``plans/unit_harness.md`` D10, Stage 1).

A module's timing is its declared :class:`~waveflow.hw.timed_phase.TimedPhase` s; calibrating them is
generated from those declarations (:mod:`waveflow.calib.phases`: which ports, their transfers off a
waveform, the pairing with pysim, the fit).  The one hand-written piece is the **workload**: a system
whose scenario carries the feature values to fit on, returned by the system's
:meth:`~waveflow.hw.hw_system.HwSystem.calibration_workload` hook.

    add_calib_steps(dag, sysm, work_dir=WORK_DIR, top=TOP)
    # ... --through calib_phases

adds the workload as a second system in the DAG (prefix ``calib_``: it shares ``codegen`` and
``csynth``), its XSI run traced, and ``calib_phases``:

1. pysim runs the workload -- every phase records its occurrences: the transfers each span starts and
   ends on, and its features;
2. the traced XSI run's VCD is decoded at each kernel's scope into its timed ports' transfers;
3. the two are joined (pysim pairs, the RTL times) and each span fitted on the clean rows, the
   held-out feature values testing the fit; the parameters are filed in each phase's ``calib_dir``,
   where it loads them from, and the report -- every row's residual -- goes to
   ``<work>/phase_calib.json``.

No per-example wrapper (``examples/stream_inband/poly_calibrate.py`` was one): a calibration is one
pass over one scenario that carries every feature value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.system_dag import _SystemStep, add_system_steps, snake


@dataclass
class CalibWorkload:
    """What :meth:`~waveflow.hw.hw_system.HwSystem.calibration_workload` returns: the *system* to run
    (the design's, with a scenario producing the feature values) and the values held out of the fit,
    per feature (``{"nwords": (48, 128)}``) -- they test it."""

    system: Any
    holdout: dict[str, tuple] = field(default_factory=dict)

    def held(self, feats: dict) -> bool:
        return any(feats.get(f) in vals for f, vals in self.holdout.items())


def timed_modules(sysm) -> list:
    """The modules of *sysm*'s DUT tree that declare timed phases -- its calibrated leaves
    (``plans/unit_harness.md`` D11: the unit tree is the module tree)."""
    out, todo = [], [sysm.dut()]
    while todo:
        m = todo.pop(0)
        if getattr(m, "timing_phases", None):
            out.append(m)
        todo.extend(getattr(m, "sub_comps", {}).values())
    return out


def calibrate_system(sysm, vcd: Path, spec, holdout=None, save: bool = True) -> dict:
    """Steps 1-3 on an un-run *sysm* whose RTL run traced *vcd* (a run of *spec*, the system top's
    :class:`~waveflow.build.system_top.SystemTopSpec`).  Returns ``{module: {phase: {span: report}}}``,
    each report :func:`~waveflow.calib.phases.fit_phase`'s plus the rows pysim had no RTL partner
    for (``missing``)."""
    from waveflow.calib.phases import fit_phase, phase_corpus, rtl_xfers

    sysm.run()
    scopes = {k.hw.name: spec.scope_of(k) for k in spec.kernels if k.hw is not None}
    out: dict = {}
    for mod in timed_modules(sysm):
        if mod.name not in scopes:
            raise RuntimeError(f"calib: '{mod.name}' declares timed phases but is not a kernel of "
                               f"the traced top")
        rtl = rtl_xfers(vcd, mod, scopes[mod.name])
        mine = out.setdefault(mod.name, {})
        for phase in mod.timing_phases.values():
            rows = phase_corpus(phase, rtl)
            done = {(r["span"], r["occurrence"]) for r in rows}
            missing = [(r.span, r.occurrence) for r in phase.records
                       if r.end is not None and (r.span, r.occurrence) not in done]
            rep = fit_phase(phase, rows, holdout=holdout, save=save)
            for span in rep:
                rep[span]["missing"] = [m for m in missing if m[0] == span]
            mine[phase.name] = rep
    return out


@dataclass(kw_only=True)
class PhaseCalibStep(_SystemStep):
    """``calib_phases``: :func:`calibrate_system` on the workload, from its traced XSI run.  Consumes
    that run's comparison -- the workload bit-exact, RTL against pysim, before any timing is fitted from
    it (timing calibration presumes functional equivalence); produces ``<work>/phase_calib.json``.  A span with no
    clean training row, or a pysim occurrence the RTL does not have, fails the step: a calibration
    is never fitted around a gap."""

    description = "Fit the declared timed phases: pysim pairs and features, the traced RTL times."
    step_name: ClassVar[str] = "phases"
    rtl: Any
    workload: CalibWorkload

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("report"), self.a("compare")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("phase_calib"): self.work / "phase_calib.json"}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        work = self.work_dir(config)
        vcd = work / f"{self.rtl.spec.top}_trace.vcd"
        if not vcd.is_file():
            raise RuntimeError(f"calib_phases: no traced run at {vcd}")
        fresh = self.sysm.fresh()                       # the pysim run the records come from
        rep = calibrate_system(fresh, vcd, self.rtl.spec, holdout=self.workload.held)
        missing = {(m, p, s): r["missing"] for m, ph in rep.items() for p, spans in ph.items()
                   for s, r in spans.items() if r["missing"]}
        out = work / "phase_calib.json"
        out.write_text(json.dumps(rep, indent=1, default=str) + "\n", encoding="utf-8")
        if missing:
            raise RuntimeError(f"calib_phases: pysim occurrences with no RTL partner: {missing}")
        return {self.a("phase_calib"): out}


def add_calib_steps(dag: BuildDag, sysm, *, work_dir, top: str | None = None,
                    prefix: str = "calib_", **kw) -> PhaseCalibStep:
    """Add *sysm*'s calibration to *dag*: its :meth:`calibration_workload` as a second system
    (*prefix*; traced; the rest of *kw* as :func:`~waveflow.build.system_dag.add_system_steps`), and
    ``<prefix>phases``.  Add it after the design's own system steps, so ``codegen`` and ``csynth``
    are shared."""
    wl = sysm.calibration_workload()
    if wl is None:
        raise ValueError(f"{type(sysm).__name__} has no calibration_workload()")
    top = top or snake(type(sysm).__name__)
    xsi = add_system_steps(dag, wl.system, work_dir=work_dir, top=top, prefix=prefix, trace=True,
                           **kw)
    step = PhaseCalibStep(sysm=wl.system, work=xsi.work, prefix=prefix, rtl=xsi.rtl, workload=wl)
    dag.add(step)
    return step


__all__ = ["CalibWorkload", "PhaseCalibStep", "add_calib_steps", "calibrate_system", "timed_modules"]
