"""calib_steps.py — the DAG steps that drive timing calibration (both levels).

Extends the timing rung (``ExtractBurstsStep`` → these) with the collect-and-fit halves from
``plans/timing_model.md``:

    ExtractBurstsStep -> CollectTimingStep -> FitTimingStep   (per-COMPONENT control residual)
                      -> CalibBusStep                         (per-PLATFORM bus-transfer model)

:class:`CalibBusStep` calibrates the *platform* half — the shared ``m_axi`` bus law read off the
memory ports; :class:`CollectTimingStep`/:class:`FitTimingStep` fit each *component*'s control
residual.  The two are calibrated from different signals (the ports vs the firings), which is what
lets the platform model be reused across accelerators.

    (CollectTimingStep / FitTimingStep are per registered TimingModel:
     collect_rtl + collect_pysim, then gen_data_frame join + fit + save params.json)

Both are driven by a **design factory** — a callable returning the built (and, for collect,
pysim-run) component tree.  The step is generic: it walks
:func:`~waveflow.hw.hw_freerun.discover_timing_models` for every attached model and never mentions a
particular design.  The example supplies the factory (how to build and run *its* pysim), exactly as
:class:`~waveflow.build.trace_steps.RtlSimStep` takes a ``prepare`` hook.

These are most useful across a **sweep** — one point cannot fit a slope — which needs the testbench's
baked run-bound + arena parameterized first (the prerequisite in ``plans/memcpy_timing_calibration.md``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Sequence

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.hw.hw_freerun import discover_timing_models


@dataclass(kw_only=True)
class CollectTimingStep(BuildStep):
    """Append one run's RTL + pysim measurements to every attached model's corpus.

    For each ``(component, model)`` on the design, calls ``model.collect_rtl(events, run_id)`` and
    ``model.collect_pysim(component.firing_records, run_id)``.  The RTL side is the
    :class:`~waveflow.build.trace_steps.ExtractBurstsStep` table; the pysim side is the
    ``firing_records`` a calibrated pysim run populated (see
    :meth:`~waveflow.hw.hw_freerun.FreeRunMod.timed_delay`).

    Construction parameters
    -----------------------
    run_pysim : callable
        Called as ``run_pysim(config)``; returns a built component tree whose pysim has been RUN — so
        ``firing_records`` are populated and the models are attached.  The design must carry
        ``calib_dir`` on its calibrated components (that is what attaches the models), which is why
        it receives the config: the platform to calibrate into is ``config.platform_info``.
    run_id : str | callable
        Scenario key for this run's folders (e.g. ``"n128"``).  A re-run of the same scenario
        overwrites, so a sweep uses one ``run_id`` per point — pass a ``callable(config) -> str`` to
        derive it from ``config.params``.

        A plain string is right for a DAG built per scenario by hand.  It is *wrong* under a sweep:
        :class:`~waveflow.build.sweep.SweepRunner` calls a zero-argument dag factory and varies the
        point through ``config.params``, so a fixed ``run_id`` would make every point overwrite the
        same corpus folder and the fit would see one point however many were run.
    events_artifact : str
        Upstream artifact naming the ``ExtractBurstsStep`` timing JSON.
    """

    description: str = "Collect a run's RTL + pysim firings into each timing model's corpus."
    params: ClassVar[dict] = {}

    run_pysim: Callable[["BuildConfig"], Any]
    run_id: "str | Callable[[BuildConfig], str]"
    events_artifact: str = "timing_events"

    def resolve_run_id(self, config: BuildConfig) -> str:
        """This point's scenario key — the string, or what the callable makes of the config."""
        return self.run_id if isinstance(self.run_id, str) else self.run_id(config)

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.events_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        # A sentinel marking the collection done; the real outputs are the per-model corpus folders
        # (declared by the models, not this step).  A *derived* run_id cannot be resolved here --
        # `produces` is read without a config, e.g. by --list-steps -- so the sentinel is named for
        # the step instead, and carries the resolved run_id in its contents.  `run` must write
        # exactly this path: the DAG checks that a declared artifact appears.
        stem = self.run_id if isinstance(self.run_id, str) else self.name
        return {"timing_collected": Path("results") / f"collected_{stem}.json"}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        run_id = self.resolve_run_id(config)
        events = json.loads(Path(artifacts[self.events_artifact]).read_text(encoding="utf-8"))
        design = self.run_pysim(config)
        found = discover_timing_models(design)
        if not found:
            raise RuntimeError(
                "CollectTimingStep: the design has no attached TimingModel — did the factory build "
                "it with `calib_dir` set on the components to calibrate?")

        report = []
        for comp, tm in found:
            tm.collect_rtl(events, run_id)
            tm.collect_pysim(getattr(comp, "firing_records", []), run_id)
            report.append({"component": tm.component,
                           "pysim_firings": len(getattr(comp, "firing_records", []))})

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / self.produces["timing_collected"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"run_id": run_id, "models": report}, indent=2) + "\n",
                       encoding="utf-8")
        return {"timing_collected": out}


@dataclass(kw_only=True)
class FitTimingStep(BuildStep):
    """Fit every attached model from its accumulated corpus, writing each ``params.json``.

    A model whose corpus does not yet join (RTL and pysim never overlapped on a feature point) is
    **skipped with a report**, not fatal — a sweep in progress has partial coverage, and forcing a
    fit there would only raise.  The skipped models keep predicting from their seed.

    Construction parameters
    -----------------------
    build_design : callable
        Called as ``build_design(config)``; returns a built component tree (no pysim run needed —
        ``fit`` reads the on-disk corpus).  Its attached models carry the ``calib_dir`` the corpus
        lives in, which is why it takes the config: that directory comes from
        ``config.platform_info``.
    after : sequence of str
        Artifacts that must exist first — normally ``("timing_collected",)``, the sentinel
        :class:`CollectTimingStep` produces.

        Fitting reads an on-disk corpus, so this step has no *data* dependency and declared none.
        In a DAG holding both, that put it **before** the step that fills the corpus: nothing
        ordered them, and the topological sort is free to emit an unconstrained step first.  It
        would then fit whatever the previous run left behind and report success.
    """

    description: str = "Fit each timing model from its corpus; write params.json."
    params: ClassVar[dict] = {}

    build_design: Callable[["BuildConfig"], Any]
    output_path: str = "results/timing_fit.json"
    after: "Sequence[str]" = ()

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return list(self.after)

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"timing_fit": Path(self.output_path)}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        found = discover_timing_models(self.build_design(config))
        fitted, skipped = [], []
        for _comp, tm in found:
            try:
                tm.fit()
                fitted.append({"component": tm.component,
                               "points": len(tm.coverage.get("matched", []))})
            except RuntimeError as exc:
                skipped.append({"component": tm.component, "reason": str(exc),
                                "coverage": tm.coverage})

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / self.output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"fitted": fitted, "skipped": skipped}, indent=2) + "\n",
                       encoding="utf-8")
        return {"timing_fit": out}


@dataclass(kw_only=True)
class CalibBusStep(BuildStep):
    """Calibrate the **platform** bus-transfer model from a traced run's ``m_axi`` ports.

    The platform half of the two-level split: measures each ``m_axi`` bundle's per-transfer span
    (:func:`~waveflow.calib.bus_model.measure_bus_span` — component-independent, read off the port)
    and accumulates it into the platform corpus at ``<platform_dir>``.  Across a sweep (this step per
    size) the corpus grows; with ``refit`` it re-fits ``mm_bus.json`` after each add, so the model is
    ready to load once ≥2 distinct sizes are present.

    Unlike :class:`CollectTimingStep` this needs no design factory — the bus law is in the trace, not
    a pysim run.  A bundle contributes to the direction(s) it carries (a read bundle → the read
    model, a write bundle → the write model).

    Construction parameters
    -----------------------
    platform_dir : str | None
        The shared platform calibration directory (``<dir>/points/`` + ``<dir>/mm_bus.json``).
        ``None`` takes it from ``config.platform_info``, which is how a sweep supplies it — the
        runner attaches the platform per point and a DAG assembled by a zero-argument factory cannot
        know it at construction.
    run_id : str | callable
        Scenario key for this run's point (e.g. ``"n128"``); a re-run overwrites it.  Pass a
        ``callable(config) -> str`` to derive it from ``config.params``: a fixed string would make
        every point of a sweep overwrite the same bus point, and the law would then be fitted from
        one size however many were measured — which is exactly the case it cannot fit at all.
    clk_freq : float
        Clock the platform model is expressed against.
    refit : bool
        Re-fit ``mm_bus.json`` from the whole corpus after adding this run (default ``True``).
    """

    description: str = "Calibrate the platform m_axi bus model from a traced run's ports."
    params: ClassVar[dict] = {}

    platform_dir: "str | None" = None
    run_id: "str | Callable[[BuildConfig], str]" = "run"
    manifest_artifact: str = "trace_manifest"
    vcd_artifact: str = "trace_vcd"
    clk_freq: float = 100e6
    refit: bool = True

    def resolve_run_id(self, config: BuildConfig) -> str:
        return self.run_id if isinstance(self.run_id, str) else self.run_id(config)

    def resolve_platform_dir(self, config: BuildConfig) -> str:
        if self.platform_dir is not None:
            return self.platform_dir
        plat = getattr(config, "platform_info", None)
        if plat is None:
            raise RuntimeError(
                "CalibBusStep has no platform_dir and the build has no platform -- the bus law is a "
                "property OF a platform, so there is nowhere for it to live. Pass platform_dir, or "
                "run with a platform attached.")
        return str(plat.dir)

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.manifest_artifact, self.vcd_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        # A per-run sentinel; the real outputs are the platform corpus + mm_bus.json (side effects,
        # since platform_dir is shared and typically outside this design's tree).  A derived run_id
        # cannot be resolved here -- `produces` is read without a config -- so the sentinel is named
        # for the step, and `run` must write exactly this path.
        stem = self.run_id if isinstance(self.run_id, str) else self.name
        return {"bus_calibrated": Path("results") / f"bus_{stem}.json"}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        from waveflow.calib.bus_model import BusCalib, measure_bus_span
        from waveflow.utils.trace import load_trace

        bt = load_trace(artifacts[self.manifest_artifact], artifacts[self.vcd_artifact])
        point = {"read": None, "write": None}
        for p in bt.manifest["boundary"]:
            if p["kind"] != "maxi":
                continue
            for direction in p.get("directions", []):
                measured = measure_bus_span(bt, p["id"], direction)
                if measured is not None:
                    point[direction] = measured

        run_id = self.resolve_run_id(config)
        bc = BusCalib(platform_dir=self.resolve_platform_dir(config), clk_freq=self.clk_freq)
        bc.add_run(run_id, read=point["read"], write=point["write"])
        fitted = bc.fit() if self.refit else {}

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / self.produces["bus_calibrated"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"run_id": run_id, "point": point,
                                   "fitted_directions": sorted(fitted)}, indent=2) + "\n",
                       encoding="utf-8")
        return {"bus_calibrated": out}


@dataclass(kw_only=True)
class SeedPlatformStep(BuildStep):
    """Make sure this build's platform exists, seeding it from an upstream one the first time.

    A project that publishes its own calibration needs its *own* platform directory, because platform
    resolution is first-match-wins on the whole directory: the moment a project owns a platform of a
    given name, the upstream one of that name stops being consulted at all.  Without seeding, a project
    that publishes one module record silently loses the bus law and infra residuals it was relying on.

    So this is create-if-absent, and it **inherits** rather than starting empty.  Idempotent: once the
    platform is there the step is a no-op, so it costs nothing to leave in a DAG.

    Why this *is* a DAG step when ``publish`` deliberately is not: the direction differs.  Publishing
    writes **upstream**, into shared infra, and is a considered "I am satisfied" act.  Seeding writes
    **downstream**, into this project's own library — ordinary setup, in the same direction as every
    other calibration step, and it touches nothing anyone else depends on.

    Parameters
    ----------
    seed_from :
        The upstream platform *name*, resolved through the usual search path (project, env, user,
        packaged).  ``None`` creates an empty platform, which is what you want only if this project is
        calibrating a genuinely new target from scratch.
    """

    description = "Create this build's platform if absent, seeding it from an upstream one."
    consumes: list = field(default_factory=list)
    produces: dict = field(default_factory=dict)

    seed_from: "str | None" = None
    params: dict = field(default_factory=dict)

    def run(self, config, **_) -> dict:
        from waveflow.calib.platform import PLATFORM_MANIFEST, Platform, platform_fallback_path
        from waveflow.calib.publish import seed_platform

        info = getattr(config, "platform_info", None)
        if info is None:
            print("  (no platform selected — nothing to seed)")
            return {}

        target = Path(info.dir)
        if (target / PLATFORM_MANIFEST).is_file() and any(
                p for p in target.iterdir() if p.name != PLATFORM_MANIFEST):
            print(f"  platform {info.name!r} already present at {target}")
            return {}

        if not self.seed_from:
            print(f"  platform {info.name!r} created empty at {target} (no seed_from given)")
            return {}

        upstream = Platform.resolve(config.platforms_root, self.seed_from,
                                    fallbacks=platform_fallback_path())
        if Path(upstream.dir).resolve() == target.resolve():
            print(f"  seed_from {self.seed_from!r} resolved to the target itself — nothing to do")
            return {}
        written = seed_platform(upstream.dir, target, force=True)
        print(f"  seeded {info.name!r} from {upstream.dir} ({len(written)} file(s))")
        return {}
