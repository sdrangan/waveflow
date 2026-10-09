"""system_dag.py — a system's whole flow on one :class:`~waveflow.build.build.BuildDag`
(``plans/system_dag.md``).

The outer DAG an example builds::

    codegen ──> csynth ──┐
                         ├──> system_xsi ──┐
    scenario ──┬─────────┘                 ├──> compare
               └──> pysim ─────────────────┘

``codegen`` is the example's (its headers, kernel tops, writer tops and ``.tcl``); everything after it
is framework, and this module holds it.

**csynth** is a composite step, :class:`CsynthTopsStep`, with one inner :class:`CsynthStep` per HLS top
-- the rebuild unit, since Vitis has no incremental csynth and every top is its own project.  A top is
fresh while the content stamp written at csynth time (:mod:`waveflow.build.rtl_digest`) still matches
the sources on disk (:func:`~waveflow.build.trace_steps.rtl_staleness`), so a ``codegen`` that rewrites
``include/`` and ``gen/`` with identical bytes re-runs no csynth: the DAG asks each step's
:meth:`~waveflow.build.build.BuildStep.is_fresh` hook late, after its upstream ran.

Who may run csynth is the ``synth`` param: ``"build"`` (the CLI default) synthesizes a stale top and
stamps it; ``"check"`` (the gate tests, ``run_system_xsi``) **fails** on a stale or missing top, naming
it and the source that changed, and never runs the toolchain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep

#: ``synth`` values: who may run csynth.
SYNTH_MODES = ("build", "check")


def rtl_rel(top: str) -> Path:
    """Where csynth leaves *top*'s Verilog, relative to the example root."""
    return Path(f"{top}_proj") / "solution1" / "syn" / "verilog"


def rtl_artifact(top: str) -> str:
    """The artifact name of *top*'s RTL in a system DAG."""
    return f"rtl_{top}"


def rtl_problem(root, top: str) -> str | None:
    """``None`` if *top*'s RTL is on disk and was built from the sources on disk; otherwise why not.

    :func:`~waveflow.build.trace_steps.rtl_staleness` with absence made a problem too (it leaves
    absence to the caller): the stamp, with its mtime fallback when there is none -- never "clean"."""
    from waveflow.build.trace_steps import rtl_staleness

    d = Path(root) / rtl_rel(top)
    if not d.is_dir() or not any(d.glob("*.v")):
        return f"no csynth RTL for {top} at {d}"
    return rtl_staleness(root, top)


def _check_synth(synth: str) -> None:
    if synth not in SYNTH_MODES:
        raise ValueError(f"synth must be one of {SYNTH_MODES}, got {synth!r}")


@dataclass(kw_only=True)
class CsynthStep(BuildStep):
    """Vitis HLS C-synthesis of one top, then its source stamp (``rtl_digest.write_stamp``).

    Fresh (:meth:`is_fresh`) while :func:`rtl_problem` finds nothing.  With ``synth="check"`` it never
    synthesizes: a stale or missing top raises, naming the top and why.  *tcl* is the csynth script,
    relative to the root (default ``gen/<top>.tcl``, :func:`~waveflow.build.composite_gen.tcl_path`);
    it runs with the root as its working directory.  *sources* are the consumed artifacts (the
    codegen's directories) -- ordering only: the hook, not their mtimes, decides."""

    description = "Vitis HLS C-synthesis of one top; fresh while its source stamp matches."
    params: ClassVar[dict] = {"synth": "build"}

    top: str
    tcl: Path | None = None
    sources: tuple[str, ...] = ("include", "gen")

    def _default_name(self) -> str:
        return f"csynth_{self.top}"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return list(self.sources)

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {rtl_artifact(self.top): rtl_rel(self.top)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return rtl_problem(config.root_dir, self.top) is None

    def tcl_file(self, root: Path) -> Path:
        from waveflow.build.composite_gen import tcl_path

        return tcl_path(root, self.top) if self.tcl is None else Path(root) / self.tcl

    def run(self, config: BuildConfig, synth: str = "build", **_: Any) -> dict[str, Any]:
        _check_synth(synth)
        root = Path(config.root_dir)
        out = {rtl_artifact(self.top): root / rtl_rel(self.top)}
        problem = rtl_problem(root, self.top)
        if synth == "check":
            if problem is not None:
                raise RuntimeError(f"{self.top}: {problem} (synth='check' does not run csynth: build "
                                   f"the example, e.g. its *_build script --through csynth)")
            return out

        from waveflow.build.rtl_digest import write_stamp
        from waveflow.toolchain.toolchain import run_vitis_hls

        import subprocess

        try:
            r = run_vitis_hls(self.tcl_file(root), work_dir=root)
        except subprocess.CalledProcessError as exc:     # the tcl exits 1 on a csynth error
            log = (exc.stdout or "") + (exc.stderr or "")
            raise RuntimeError(f"csynth of {self.top} failed:\n{log[-6000:]}") from exc
        log = (r.stdout or "") + (r.stderr or "")
        if "WAVEFLOW_CSYNTH_OK" not in log:
            raise RuntimeError(f"csynth of {self.top} failed:\n{log[-6000:]}")
        write_stamp(root, self.top)
        return out


@dataclass(kw_only=True)
class CsynthTopsStep(BuildStep):
    """``csynth``: every HLS top of a system, one inner :class:`CsynthStep` each.

    Its consumed artifacts enter the inner DAG as :class:`~waveflow.build.build.SourceStep`\\ s, and
    the inner DAG runs **without force**: each top decides for itself, so editing one top's generated
    source re-runs that csynth and reuses the others' RTL.  Fresh when every top is
    (:meth:`is_fresh`), so a run with nothing to do does not enter at all and the cascade stops here.
    *tcls* overrides a top's csynth script (``{top: path relative to the root}``)."""

    description = "C-synthesis of every HLS top, one inner step per top (each re-runs only when stale)."
    params: ClassVar[dict] = {"synth": "build"}

    tops: tuple[str, ...]
    tcls: dict[str, Path] = field(default_factory=dict)
    sources: tuple[str, ...] = ("include", "gen")

    def __post_init__(self) -> None:
        self.tops = tuple(self.tops)
        super().__post_init__()

    def _default_name(self) -> str:
        return "csynth"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return list(self.sources)

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {rtl_artifact(t): rtl_rel(t) for t in self.tops}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return all(rtl_problem(config.root_dir, t) is None for t in self.tops)

    def inner_dag(self, inputs: dict[str, Path]) -> BuildDag:
        """The inner DAG: the consumed artifacts as sources, then one :class:`CsynthStep` per top."""
        dag = BuildDag()
        for name in self.sources:
            dag.add(SourceStep(artifact=name, path=Path(inputs[name])))
        for t in self.tops:
            dag.add(CsynthStep(top=t, tcl=self.tcls.get(t), sources=self.sources))
        return dag

    def run(self, config: BuildConfig, synth: str = "build", **inputs: Any) -> dict[str, Any]:
        _check_synth(synth)
        root = Path(config.root_dir)
        inner = BuildConfig(root_dir=root, vitis_version=config.vitis_version,
                            params={**config.params, "synth": synth})
        results = self.inner_dag(inputs).run(inner)
        #: Per top: "ran" / "fresh" / the failure -- what the last entry did, for the caller to show.
        self.last_run = {n: ("fresh" if r.skipped else "ran") if r.success else r.message
                         for n, r in results.items() if n.startswith("csynth_")}
        bad = [r.message for r in results.values() if not r.success]
        if bad:
            raise RuntimeError("; ".join(bad))
        return {rtl_artifact(t): root / rtl_rel(t) for t in self.tops}



# ---------------------------------------------------------------------------------------------------
# scenario, pysim, compare (plans/system_dag.md Stage 3)
# ---------------------------------------------------------------------------------------------------

def _host(sysm):
    from waveflow.build.system_xsi import discover

    return discover(sysm)[1]


@dataclass(kw_only=True)
class _SystemStep(BuildStep):
    """A framework step about one system: *sysm* (the pysim system object), its workspace *work*
    (relative to the root, or absolute), and the *prefix* naming its artifacts -- so two systems can
    share one DAG.  Never fresh: what each reads includes Python and C++ the DAG cannot see, and every
    one is seconds (the XSI run re-runs anyway).  Only csynth decides freshness by content."""

    params: ClassVar[dict] = {}

    sysm: Any
    work: Path
    prefix: str = ""

    def __post_init__(self) -> None:
        self.work = Path(self.work)
        super().__post_init__()

    def _default_name(self) -> str:
        return self.prefix + self.step_name

    def a(self, name: str) -> str:
        """This system's artifact *name*."""
        return self.prefix + name

    def work_dir(self, config: BuildConfig) -> Path:
        return self.work if self.work.is_absolute() else Path(config.root_dir) / self.work

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False


@dataclass(kw_only=True)
class ScenarioStep(_SystemStep):
    """``scenario``: the host writes its scenario bundle (``SwHost.write_scenario``) -- the one file the
    pysim run and the XSI run both read."""

    description = "The host's scenario bundle: the one file pysim and the RTL run both read."
    step_name: ClassVar[str] = "scenario"

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("scenario"): self.work / "scenario"}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        path = self.work_dir(config) / "scenario"
        host = _host(self.sysm)
        host.scenario = path.as_posix()
        host.write_scenario(path)
        return {self.a("scenario"): path}


@dataclass(kw_only=True)
class PysimStep(_SystemStep):
    """``pysim``: the system run in pysim from the scenario file, each host endpoint's trace dumped to
    ``<work>/pysim_traces/``, and the run's length in host clock cycles to ``<work>/pysim.json``.  Needs
    no toolchain.  Runs *sysm* -- a system object runs once, so a DAG holding one runs this once."""

    description = "The system in pysim, from the scenario: the host's traces and the cycle count."
    step_name: ClassVar[str] = "pysim"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("scenario")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("pysim_traces"): self.work / "pysim_traces",
                self.a("pysim"): self.work / "pysim.json"}

    def run(self, config: BuildConfig, **inputs: Any) -> dict[str, Any]:
        import json
        import shutil

        work = self.work_dir(config)
        traces = work / "pysim_traces"
        shutil.rmtree(traces, ignore_errors=True)        # a stale trace would describe another run
        host = _host(self.sysm)
        host.scenario = Path(inputs[self.a("scenario")]).as_posix()
        host.trace_dir = traces.as_posix()
        self.sysm.run()
        report = work / "pysim.json"
        report.write_text(json.dumps({"cycles": self.sysm.sim.env.now / host.clk.period}, indent=2)
                          + "\n", encoding="utf-8")
        return {self.a("pysim_traces"): traces, self.a("pysim"): report}


@dataclass(kw_only=True)
class CompareStep(_SystemStep):
    """``compare``: every host endpoint's trace, RTL against pysim, file for file
    (:func:`~waveflow.build.system_xsi.compare_traces`).  The mismatches go to ``<work>/compare.json``
    (empty: pass); any mismatch then **fails** the step, so a run whose host disagrees never reads as
    a pass."""

    description = "The host's traces, RTL against pysim, file for file (the mismatches; empty: pass)."
    step_name: ClassVar[str] = "compare"
    traces_from: str = "traces"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a(self.traces_from), self.a("pysim_traces")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("compare"): self.work / "compare.json"}

    def run(self, config: BuildConfig, **inputs: Any) -> dict[str, Any]:
        import json

        from waveflow.build.system_xsi import compare_traces

        bad = compare_traces(Path(inputs[self.a(self.traces_from)]),
                             Path(inputs[self.a("pysim_traces")]))
        out = self.work_dir(config) / "compare.json"
        out.write_text(json.dumps({"trace_mismatches": bad}, indent=2) + "\n", encoding="utf-8")
        if bad:
            raise RuntimeError(f"the host's RTL traces differ from pysim's: {bad}")
        return {self.a("compare"): out}


__all__ = ["CompareStep", "CsynthStep", "CsynthTopsStep", "PysimStep", "SYNTH_MODES", "ScenarioStep",
           "rtl_artifact", "rtl_problem", "rtl_rel"]
