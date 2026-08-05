"""sweep.py — run a design at every point of a parameter grid, and keep the measurements.

A calibration corpus is built by sweeping: elaborate a design at each point of a grid, run it through
the build DAG, and let the steps file what they measured.  Two examples did that with ~150 lines
each, of which about fifteen were about the design; a third (``examples/mem_copy``) did it again for
timing.  The rest was the same ten concerns written three times -- config construction, failure
isolation, incremental save, resume, dry-run, progress, exit code.

Not modelled on ``GridSearchCV``, despite the surface resemblance.  That is an *optimizer*: it
cross-validates, scores, and hands back ``best_params_``.  A synthesis sweep is a **census** -- it
measures every point to build a corpus, and there is no score to maximize and no held-out fold at
sweep time.  The right analogue is ``ParameterGrid``, the iterator, plus a runner owning what sklearn
has no concept of because its estimators fit in milliseconds while these points cost ~45 seconds of
Vitis each.

See ``plans/sweep_runner.md``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


class ParamGrid:
    """The points a sweep visits: a Cartesian product over named axes, in **declaration order**.

    ```python
    ParamGrid(vlen=(512, 1024), dwid=(32, 64))
    # vlen is the OUTER loop -- {vlen:512,dwid:32}, {vlen:512,dwid:64}, {vlen:1024,dwid:32}, ...
    ```

    Declaration order *is* iteration order, so a grid reads the way it runs.  That matters more than
    it looks: the two existing sweeps disagree about it (one iterates its first-named axis fastest and
    the other slowest), and a runner that imposed its own order would silently reorder an interrupted
    sweep's remaining work.

    **A single-value axis is a constant.**  ``samp_i=(2,)`` contributes one value to every point and no
    branching, which is how a design that holds a parameter fixed says so without a second concept for
    it.
    """

    def __init__(self, _workload: "Sequence[str]" = (), **axes: "Sequence[Any]") -> None:
        empty = [k for k, v in axes.items() if not len(tuple(v))]
        if empty:
            raise ValueError(
                f"axis {empty} has no values; a grid with an empty axis yields no points at all, "
                f"which is almost never what was meant — pass a single-value tuple for a constant")
        unknown = [w for w in _workload if w not in axes]
        if unknown:
            raise ValueError(f"_workload names {unknown}, which are not axes of this grid")
        self.axes: dict = {k: tuple(v) for k, v in axes.items()}
        #: Axes that vary the **workload** rather than the hardware.
        #:
        #: A build axis is a ``HwParam``: changing it produces different hardware, so every stage from
        #: elaboration onward must re-run.  A workload axis is a runtime input -- the hardware is
        #: unchanged and only the simulation repeats.  Utilization does not depend on workload at all
        #: (``ResourceModel.get_params`` drops ``**runtime`` for exactly that reason), while a timing
        #: corpus is mostly workload points against one build.  A runner that could not tell them
        #: apart would re-synthesize for a change in ``nwords``, turning a seconds-long pysim sweep
        #: into an hours-long one.
        self.workload: frozenset = frozenset(_workload)

    @property
    def build_axes(self) -> dict:
        """Axes whose change requires re-elaboration and re-synthesis."""
        return {k: v for k, v in self.axes.items() if k not in self.workload}

    @property
    def workload_axes(self) -> dict:
        return {k: v for k, v in self.axes.items() if k in self.workload}

    def __len__(self) -> int:
        n = 1
        for v in self.axes.values():
            n *= len(v)
        return n

    def __iter__(self) -> "Iterator[dict]":
        names = list(self.axes)
        for combo in product(*(self.axes[n] for n in names)):
            yield dict(zip(names, combo))

    def label(self, point: "Mapping[str, Any]") -> str:
        """A filesystem- and log-safe name for one point.

        Derived from the point rather than written per example, so the summary key and the progress
        line cannot disagree about which point a record belongs to.  Only the axes appear -- a
        constant contributes nothing to distinguishing points and would only pad every label.
        """
        varying = [k for k, v in self.axes.items() if len(v) > 1]
        parts = [f"{k}{_slug(point[k])}" for k in (varying or list(self.axes)) if k in point]
        return "_".join(parts) or "point"

    def subset(self, **overrides: "Sequence[Any] | None") -> "ParamGrid":
        """A grid with some axes restricted — what a ``--ntap 8 16`` flag produces.

        ``None`` leaves an axis alone, so a CLI can pass every axis through without special-casing
        the ones the user did not mention.
        """
        axes = dict(self.axes)
        for k, v in overrides.items():
            if v is None:
                continue
            if k not in axes:
                raise ValueError(f"{k!r} is not an axis of this grid ({sorted(axes)})")
            axes[k] = tuple(v)
        return ParamGrid(_workload=tuple(self.workload), **axes)

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.axes.items())
        return f"ParamGrid({inner})"


def _slug(value: Any) -> str:
    """A label fragment: ``True``/``False`` read better than ``1``/``0`` in a sweep log."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).replace(" ", "").replace("/", "-")


# ---------------------------------------------------------------------------
# Stages — a point may need more than one run
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage:
    """One pass over a point: run the DAG up to *through*.

    A resource sweep is one stage per point -- synthesize, attribute, file.  A timing sweep is two,
    at deliberately different cadences: :class:`~waveflow.calib.timing_model.TimingModel` keeps its
    ``rtl/`` and ``pysim/`` trees apart because RTL is Vitis-expensive and pysim is every-edit-cheap,
    and joins them at fit time on the feature point rather than on the run id.

    *when* skips the stage for a point, which is how the expensive side covers a subset without a
    special mode.  *use_platform* off is for a dry run: nothing was synthesized, so there is no report
    to file, and attaching a platform would only invite a half-written library.
    """

    through: str
    name: str = ""
    when: "Callable[[Mapping[str, Any]], bool] | None" = None
    use_platform: bool = True

    @property
    def label(self) -> str:
        return self.name or self.through

    def applies_to(self, point: "Mapping[str, Any]") -> bool:
        return True if self.when is None else bool(self.when(point))


@dataclass
class SweepResult:
    """What a sweep did: one entry per point, each a map of stage label -> outcome."""

    points: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)
    total_seconds: float = 0.0
    complete: bool = False

    @property
    def failures(self) -> list:
        return [(lbl, st, rec.get("error", ""))
                for lbl, stages in sorted(self.points.items())
                for st, rec in sorted(stages.items()) if not rec.get("ok")]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_json(self) -> dict:
        return {"grid": self.grid, "complete": self.complete,
                "n_points": len(self.points),
                "total_seconds": round(self.total_seconds, 1),
                "points": self.points}


class SweepRunner:
    """Run a design at every point of a grid, and keep what the steps measured.

    Owns the concerns three hand-written sweeps each reinvented: config construction, per-point
    failure isolation, incremental save, resume, progress and the exit code.  What an example supplies
    is its axes, its DAG factory and its platform -- the parts that are about the design.

    **The summary is a log, not a corpus.**  It records what was attempted, what failed, how long it
    took, and *pointers* to what was filed.  The numbers live in the record store; duplicating them
    here would make a second copy with the untracked one easy to leave stale, which is the failure the
    whole calibration tier exists to prevent.
    """

    def __init__(self, *, dag_factory, root_dir, summary=None, platform=None, platforms_root=None,
                 part=None, clk_freq=None, extra_params=None) -> None:
        self.dag_factory = dag_factory
        self.root_dir = Path(root_dir)
        self.summary = Path(summary) if summary else self.root_dir / "results" / "sweep.json"
        self.platform = platform
        self.platforms_root = platforms_root
        self.part = part
        self.clk_freq = clk_freq
        #: Params every point carries that are not swept -- ``live_output=False`` and friends.
        self.extra_params = dict(extra_params or {})

    # -- one point, one stage ------------------------------------------------
    def _config(self, point: "Mapping[str, Any]", stage: Stage):
        from waveflow.build.build import BuildConfig

        kw: dict = {"root_dir": self.root_dir, "params": {**point, **self.extra_params}}
        if stage.use_platform and self.platform is not None:
            kw.update(platform=self.platform, platforms_root=self.platforms_root,
                      part=self.part, clk_freq=self.clk_freq)
        return BuildConfig(**kw)

    def run_stage(self, point: "Mapping[str, Any]", stage: Stage) -> dict:
        """Run one stage of one point.  **Never raises.**

        A point that blows up is data about the design space, not a reason to lose the other fifteen
        -- the behaviour all three hand-written sweeps chose independently.  The exception becomes the
        record's ``error`` and the sweep carries on.
        """
        started = time.perf_counter()
        before = self._store_fingerprint() if stage.use_platform else {}
        try:
            results = self.dag_factory().run(self._config(point, stage),
                                             through=stage.through, force=True)
        except Exception as exc:                    # noqa: BLE001 - a failure is a datapoint
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "elapsed": round(time.perf_counter() - started, 2)}

        failed = {n: r.message for n, r in results.items() if not r.success}
        rec: dict = {"ok": not failed, "elapsed": round(time.perf_counter() - started, 2)}
        if failed:
            rec["error"] = "; ".join(f"{n}: {m}" for n, m in failed.items())
        elif stage.use_platform:
            filed = sorted(self._filed_since(before))
            if filed:
                rec["filed"] = filed
        return rec

    # -- what a stage filed --------------------------------------------------
    def _store_dir(self) -> "Path | None":
        if self.platform is None or self.platforms_root is None:
            return None
        return Path(self.platforms_root) / str(self.platform) / "modules"

    def _store_fingerprint(self) -> dict:
        """``{module key: total records}`` -- enough to notice an *append*, not only a new key.

        A re-measured configuration files another record under a key that already exists, so
        comparing key sets alone would report that the point filed nothing.
        """
        root = self._store_dir()
        if root is None or not root.is_dir():
            return {}
        out: dict = {}
        for jsonl in root.rglob("records.jsonl"):
            key = jsonl.parent.parent.name
            try:
                with jsonl.open(encoding="utf-8") as fh:
                    out[key] = out.get(key, 0) + sum(1 for _ in fh)
            except OSError:
                continue
        return out

    def _filed_since(self, before: dict) -> list:
        after = self._store_fingerprint()
        return [k for k, n in after.items() if n != before.get(k, 0)]

    # -- the sweep -----------------------------------------------------------
    def run(self, grid: ParamGrid, stages: "Sequence[Stage] | Stage", *, resume: bool = False,
            verbose: bool = True) -> SweepResult:
        """Run every point through every applicable stage.

        With *resume*, a ``(point, stage)`` already recorded ``ok`` is skipped -- **per stage** rather
        than per point, so re-running after a change to the cheap side does not re-run the expensive
        one.
        """
        stages = [stages] if isinstance(stages, Stage) else list(stages)
        result = SweepResult(grid={k: list(v) for k, v in grid.axes.items()})
        if resume:
            result.points = _load_points(self.summary)
            done = sum(1 for st in result.points.values() for r in st.values() if r.get("ok"))
            if verbose and done:
                print(f"resuming: {done} (point, stage) pair(s) already recorded ok")

        started = time.perf_counter()
        total = len(grid)
        for i, point in enumerate(grid, 1):
            label = grid.label(point)
            per = result.points.setdefault(label, {})
            for stage in stages:
                if not stage.applies_to(point):
                    continue
                if resume and per.get(stage.label, {}).get("ok"):
                    if verbose:
                        print(f"[{i}/{total}] {label} {stage.label} - skipped (already ok)")
                    continue
                if verbose:
                    print(f"[{i}/{total}] {label} {stage.label} ...", flush=True)
                rec = self.run_stage(point, stage)
                per[stage.label] = rec
                if verbose:
                    tail = (f"FAILED  {rec.get('error', '')[:110]}" if not rec["ok"] else
                            f"ok  {rec['elapsed']:.1f}s"
                            + (f"  filed {len(rec['filed'])}" if rec.get("filed") else ""))
                    print(f"    {tail}")
                # Incremental: a crash costs one stage, not the sweep.  Writing only at the end means
                # an interruption at point 15 saves nothing AND leaves the previous run's file in
                # place -- a stale summary that reads as a fresh one.
                result.total_seconds = time.perf_counter() - started
                _save(self.summary, result)

        result.total_seconds = time.perf_counter() - started
        result.complete = len(result.points) == total
        _save(self.summary, result)
        if verbose:
            bad = result.failures
            print(f"\n{total - len({b[0] for b in bad})}/{total} point(s) clean, "
                  f"{len(bad)} stage failure(s), {result.total_seconds:.0f}s")
            for lbl, st, err in bad:
                print(f"  FAILED {lbl} {st}: {err[:140]}")
            print(f"summary -> {self.summary}")
        return result


def _load_points(path: Path) -> dict:
    if not Path(path).is_file():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("points", {})
    except (OSError, ValueError):
        return {}


def _save(path: Path, result: SweepResult) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")


__all__ = ["ParamGrid", "Stage", "SweepResult", "SweepRunner"]
