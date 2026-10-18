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
from waveflow.build.system_dag import _SystemStep, add_system_steps, default_top


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
    #: Where the fits are filed (``plans/unit_harness.md`` D7): a
    #: :class:`~waveflow.calib.record_store.ModuleStore` -- one timing record per calibrated module,
    #: under its identity -- or ``None`` (each phase's per-span files in its ``calib_dir``).
    store: Any = None
    #: The workload's digest (:func:`~waveflow.calib.phases.scenario_digest`), filed with the record:
    #: what a repeat compares against.
    workload_digest: str = ""

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
        import time

        t0 = time.perf_counter()
        fresh = self.sysm.fresh()                       # the pysim run the records come from
        rep = calibrate_system(fresh, vcd, self.rtl.spec, holdout=self.workload.held,
                               save=self.store is None)
        missing = {(m, p, s): r["missing"] for m, ph in rep.items() for p, spans in ph.items()
                   for s, r in spans.items() if r["missing"]}
        out = work / "phase_calib.json"
        out.write_text(json.dumps(rep, indent=1, default=str) + "\n", encoding="utf-8")
        if missing:
            raise RuntimeError(f"calib_phases: pysim occurrences with no RTL partner: {missing}")
        if self.store is not None:
            # Filed under an UN-RUN copy's identity, which is what a lookup computes: a module's own
            # run-time state (a job counter) must not decide where its calibration lives.
            self._file(config, self.sysm.fresh(), rep, time.perf_counter() - t0)
        return {self.a("phase_calib"): out}

    def _file(self, config: BuildConfig, sysm, rep: dict, seconds: float) -> None:
        """One timing record per calibrated module, under its identity, with its provenance."""
        from waveflow.build.hls_ip import csynth_part
        from waveflow.build.system_dag import rtl_rel
        from waveflow.calib.phases import phase_record

        root = Path(config.root_dir)
        for mod in timed_modules(sysm):
            part = csynth_part((root / rtl_rel(type(mod).cpp_kernel_name)).parents[1]) or ""
            rec, ident = phase_record(mod, rep[mod.name], workload=self.workload_digest, part=part,
                                      period_ns=float(mod.clk.period) * 1e9,
                                      tool=f"vitis {config.vitis_version or ''}".strip(),
                                      cost_seconds=seconds)
            self.store.append(rec, identity=ident)


@dataclass(kw_only=True)
class PhaseCalibCachedStep(_SystemStep):
    """``calib_phases`` when the store already holds this calibration: every calibrated module has a
    record under its identity, measured on this very workload, verified against the module -- so
    nothing is simulated.  Writes ``<work>/phase_calib.json`` naming the records it found."""

    description = "The calibration is in the store for this identity and workload: nothing to run."
    step_name: ClassVar[str] = "phases"
    hits: Any = None

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return []

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("phase_calib"): self.work / "phase_calib.json"}

    def _report(self) -> str:
        return json.dumps({"cache_hit": True, "records": self.hits}, indent=1) + "\n"

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        """Fresh while its report already names these records: the store was asked when the DAG was
        built, so there is nothing left to decide (Stage 6)."""
        out = self.work_dir(config) / "phase_calib.json"
        return out.is_file() and out.read_text(encoding="utf-8") == self._report()

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        out = self.work_dir(config) / "phase_calib.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self._report(), encoding="utf-8")
        return {self.a("phase_calib"): out}


@dataclass(kw_only=True)
class MemCalibStep(_SystemStep):
    """``<prefix>mem_timing``: an on-chip memory's timing (:mod:`waveflow.calib.mem_timing`), from its
    harness's two runs (:func:`~waveflow.hw.harness.memory_harness`) -- the script's op timings, RTL
    and pysim -- filed under the memory's identity in *store*.  Consumes the run's comparison: the read
    data equal, RTL against pysim, before any timing is fitted.  The harness's memory must load no
    stored timing (the fit is what the memory adds over the rest of the harness)."""

    description = "Fit an on-chip memory's per-direction costs and serialization from its harness."
    step_name: ClassVar[str] = "mem_timing"
    store: Any

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("compare")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("mem_timing"): self.work / "mem_timing.json"}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import hashlib

        from waveflow.calib.mem_timing import fit_mem_timing, mem_timing_record
        from waveflow.hw.hw_system import dut_members
        from waveflow.simulation.mm_tb import timing_dir
        from waveflow.utils.burst_io import read_burst_bundle

        work = self.work_dir(config)
        (ran,) = dut_members(self.sysm.dut())
        if getattr(ran, "_mem_timing", None) is not None:
            raise RuntimeError("mem_timing: the harness's memory loaded a stored timing; fit from one "
                               "that loads none")
        ops = list(self.sysm.script)
        rtl = read_burst_bundle(timing_dir(work / "traces"))
        base = read_burst_bundle(timing_dir(work / "pysim_traces"))
        if len(rtl) != len(ops) or len(base) != len(ops):
            raise RuntimeError(f"mem_timing: {len(ops)} ops, {len(rtl)} RTL and {len(base)} pysim times")
        payload = fit_mem_timing(ops, base, rtl)
        digest = hashlib.sha256(b"".join(bytes(memoryview(o.astype("<u8"))) for o in ops)).hexdigest()[:16]
        (mem,) = dut_members(self.sysm.fresh().dut())        # un-run: the identity a lookup computes
        rec, ident = mem_timing_record(mem, payload, workload=digest,
                                       period_ns=float(mem.clk.period) * 1e9,
                                       tool=f"vitis {config.vitis_version or ''}".strip())
        from waveflow.calib.mem_timing import stored_mem_timing

        newest = stored_mem_timing(self.store, mem, workload=digest)
        if newest is None or newest.payload != rec.payload:   # a repeat measuring the same: no copy
            self.store.append(rec, identity=ident)
        out = work / "mem_timing.json"
        out.write_text(json.dumps({"key": ident.key, **payload}, indent=1) + "\n", encoding="utf-8")
        return {self.a("mem_timing"): out}


def add_calib_steps(dag: BuildDag, sysm, *, work_dir, top: str | None = None,
                    prefix: str = "calib_", store=None, **kw):
    """Add *sysm*'s calibration to *dag*: its :meth:`calibration_workload` as a second system
    (*prefix*; traced; the rest of *kw* as :func:`~waveflow.build.system_dag.add_system_steps`), and
    ``<prefix>phases``.  Add it after the design's own system steps, so ``codegen`` and ``csynth``
    are shared.

    With a *store* (a :class:`~waveflow.calib.record_store.ModuleStore`, D7) the fits are filed there,
    one record per calibrated module under its identity -- and the store is asked first: when every
    calibrated module already has a record measured on this workload (same digest, provenance
    verified against the module), only a :class:`PhaseCalibCachedStep` is added, and a repeated
    calibration runs no simulation."""
    from waveflow.calib.phases import module_identity, scenario_digest, stored_phases

    wl = sysm.calibration_workload()
    if wl is None:
        raise ValueError(f"{type(sysm).__name__} has no calibration_workload()")
    # It writes the records its modules would load: its freshness must not follow them (Stage 6).
    wl.system._wf_calibrating = True
    top = top or default_top(sysm)
    digest = scenario_digest(wl.system) if store is not None else ""
    if store is not None:
        mods = timed_modules(wl.system)
        found = [stored_phases(store, m, workload=digest) for m in mods]
        if mods and all(r is not None for r in found):
            hits = [{"module": m.name, "key": module_identity(m).key, "workload": digest}
                    for m in mods]
            step = PhaseCalibCachedStep(sysm=wl.system, work=Path(work_dir) / f"{prefix}{top}",
                                        prefix=prefix, hits=hits)
            dag.add(step)
            return step
    xsi = add_system_steps(dag, wl.system, work_dir=work_dir, top=top, prefix=prefix, trace=True,
                           **kw)
    step = PhaseCalibStep(sysm=wl.system, work=xsi.work, prefix=prefix, rtl=xsi.rtl, workload=wl,
                          store=store, workload_digest=digest)
    dag.add(step)
    return step


__all__ = ["CalibWorkload", "MemCalibStep", "PhaseCalibCachedStep", "PhaseCalibStep", "add_calib_steps",
           "calibrate_system", "timed_modules"]
