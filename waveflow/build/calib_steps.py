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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar

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
        Returns a built component tree whose pysim has been RUN — so ``firing_records`` are populated
        and the models are attached.  The design must carry ``calib_dir`` on its calibrated
        components (that is what attaches the models).
    run_id : str
        Scenario key for this run's folders (e.g. ``"n128"``).  A re-run of the same scenario
        overwrites, so a sweep uses one ``run_id`` per point.
    events_artifact : str
        Upstream artifact naming the ``ExtractBurstsStep`` timing JSON.
    """

    description: str = "Collect a run's RTL + pysim firings into each timing model's corpus."
    params: ClassVar[dict] = {}

    run_pysim: Callable[[], Any]
    run_id: str
    events_artifact: str = "timing_events"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.events_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        # A sentinel marking the collection done for this run_id; the real outputs are the per-model
        # corpus folders (declared by the models, not this step).
        return {"timing_collected": Path("results") / f"collected_{self.run_id}.json"}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        events = json.loads(Path(artifacts[self.events_artifact]).read_text(encoding="utf-8"))
        design = self.run_pysim()
        found = discover_timing_models(design)
        if not found:
            raise RuntimeError(
                "CollectTimingStep: the design has no attached TimingModel — did the factory build "
                "it with `calib_dir` set on the components to calibrate?")

        report = []
        for comp, tm in found:
            tm.collect_rtl(events, self.run_id)
            tm.collect_pysim(getattr(comp, "firing_records", []), self.run_id)
            report.append({"component": tm.component,
                           "pysim_firings": len(getattr(comp, "firing_records", []))})

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / "results" / f"collected_{self.run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"run_id": self.run_id, "models": report}, indent=2) + "\n",
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
        Returns a built component tree (no pysim run needed — ``fit`` reads the on-disk corpus).  Its
        attached models carry the ``calib_dir`` the corpus lives in.
    """

    description: str = "Fit each timing model from its corpus; write params.json."
    params: ClassVar[dict] = {}

    build_design: Callable[[], Any]
    output_path: str = "results/timing_fit.json"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return []

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"timing_fit": Path(self.output_path)}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        found = discover_timing_models(self.build_design())
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
    platform_dir : str
        The shared platform calibration directory (``<dir>/points/`` + ``<dir>/mm_bus.json``).
    run_id : str
        Scenario key for this run's point (e.g. ``"n128"``); a re-run overwrites it.
    clk_freq : float
        Clock the platform model is expressed against.
    refit : bool
        Re-fit ``mm_bus.json`` from the whole corpus after adding this run (default ``True``).
    """

    description: str = "Calibrate the platform m_axi bus model from a traced run's ports."
    params: ClassVar[dict] = {}

    platform_dir: str
    run_id: str
    manifest_artifact: str = "trace_manifest"
    vcd_artifact: str = "trace_vcd"
    clk_freq: float = 100e6
    refit: bool = True

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.manifest_artifact, self.vcd_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        # A per-run sentinel; the real outputs are the platform corpus + mm_bus.json (side effects,
        # since platform_dir is shared and typically outside this design's tree).
        return {"bus_calibrated": Path("results") / f"bus_{self.run_id}.json"}

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

        bc = BusCalib(platform_dir=self.platform_dir, clk_freq=self.clk_freq)
        bc.add_run(self.run_id, read=point["read"], write=point["write"])
        fitted = bc.fit() if self.refit else {}

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        out = root / "results" / f"bus_{self.run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"run_id": self.run_id, "point": point,
                                   "fitted_directions": sorted(fitted)}, indent=2) + "\n",
                       encoding="utf-8")
        return {"bus_calibrated": out}
