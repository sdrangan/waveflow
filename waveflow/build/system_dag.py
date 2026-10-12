"""system_dag.py — a system's whole flow on one :class:`~waveflow.build.build.BuildDag`
(``plans/system_dag.md``).

The outer DAG an example builds::

    codegen ──> csynth ──┬──> system_rtl ──┬──────────────────┐
                         │                 │                  ├──> system_xsi ──┐
    scenario ──┬─────────┼─────────────────┼──────────────────┘                 ├──> compare
               └──> pysim┼─────────────────┼────────────────────────────────────┘
                         │                 ├──> leaf_resources ──┐
                         └──> kernel_resources ──────────────────┴──> resource_report
                                           └──> system_synth   (opt-in; resource_report reads it)

The resource half (``plans/system_resources.md``): ``kernel_resources`` (:class:`KernelResourcesStep`)
attributes each kernel's csynth report and files the records; ``leaf_resources``
(:class:`LeafResourcesStep`) looks every leaf / IP configuration up, synthesizing the missing ones out
of context when asked; ``system_synth`` (:class:`SystemSynthStep`) synthesizes the whole top in
Vivado -- minutes, so opt-in; ``resource_report`` (:class:`ResourceReportStep`) is the estimate, row
by row, with the measured total beside it once ``system_synth`` has run.

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

**system_rtl**, **scenario**, **pysim**, **system_xsi** and **compare** (:class:`SystemRtlStep` -- an
inner DAG of the crossbar IP and the system top, so csynth and it are everything that makes Verilog --
:class:`ScenarioStep`, :class:`PysimStep`, :class:`SystemXsiStep` -- an inner DAG of the host harness
and the XSI run -- and :class:`CompareStep`) are never fresh: each reads Python and C++ the DAG cannot see, and each is
seconds.  pysim and XSI read the same scenario file; ``--through pysim`` needs no Vivado.
:func:`add_system_steps` adds all of them for one system object; the run is read back with
:func:`waveflow.build.system_xsi.load_run`.
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


def report_rel(top: str) -> Path:
    """*top*'s Vitis solution directory (its csynth report), relative to the example root."""
    return Path(f"{top}_proj") / "solution1"


def report_artifact(top: str) -> str:
    """The artifact name of *top*'s csynth report (its solution directory)."""
    return f"report_{top}"


def seconds_rel(top: str) -> Path:
    """Where :class:`CsynthStep` records how long *top*'s csynth took."""
    return report_rel(top) / "waveflow_csynth.json"


def seconds_artifact(top: str) -> str:
    return f"synth_seconds_{top}"


def _csynth_products(top: str) -> dict:
    return {rtl_artifact(top): rtl_rel(top), report_artifact(top): report_rel(top),
            seconds_artifact(top): seconds_rel(top)}


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
        return _csynth_products(self.top)

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return rtl_problem(config.root_dir, self.top) is None

    def tcl_file(self, root: Path) -> Path:
        from waveflow.build.composite_gen import tcl_path

        return tcl_path(root, self.top) if self.tcl is None else Path(root) / self.tcl

    def run(self, config: BuildConfig, synth: str = "build", **_: Any) -> dict[str, Any]:
        import json
        import time

        _check_synth(synth)
        root = Path(config.root_dir)
        out = {k: root / v for k, v in _csynth_products(self.top).items()}
        problem = rtl_problem(root, self.top)
        if synth == "check":
            if problem is not None:
                raise RuntimeError(f"{self.top}: {problem} (synth='check' does not run csynth: build "
                                   f"the example, e.g. its *_build script --through csynth)")
            return out

        from waveflow.build.rtl_digest import write_stamp
        from waveflow.toolchain.toolchain import run_vitis_hls

        import subprocess

        t0 = time.perf_counter()
        try:
            r = run_vitis_hls(self.tcl_file(root), work_dir=root)
        except subprocess.CalledProcessError as exc:     # the tcl exits 1 on a csynth error
            log = (exc.stdout or "") + (exc.stderr or "")
            raise RuntimeError(f"csynth of {self.top} failed:\n{log[-6000:]}") from exc
        log = (r.stdout or "") + (r.stderr or "")
        if "WAVEFLOW_CSYNTH_OK" not in log:
            raise RuntimeError(f"csynth of {self.top} failed:\n{log[-6000:]}")
        from waveflow.build.composite_gen import check_maxi_lowered

        check_maxi_lowered(root, self.top)
        write_stamp(root, self.top)
        # How long the synthesis took: the cost a record of it carries (``cost_seconds``).
        secs = root / seconds_rel(self.top)
        secs.parent.mkdir(parents=True, exist_ok=True)
        secs.write_text(json.dumps({"synth_seconds": round(time.perf_counter() - t0, 3)}) + "\n",
                        encoding="utf-8")
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
        return {k: v for t in self.tops for k, v in _csynth_products(t).items()}

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
        return {k: root / v for t in self.tops for k, v in _csynth_products(t).items()}



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

        out = self.work_dir(config) / "compare.json"
        out.unlink(missing_ok=True)                      # a previous verdict must not outlive this run
        bad = compare_traces(Path(inputs[self.a(self.traces_from)]),
                             Path(inputs[self.a("pysim_traces")]))
        out.write_text(json.dumps({"trace_mismatches": bad}, indent=2) + "\n", encoding="utf-8")
        if bad:
            raise RuntimeError(f"the host's RTL traces differ from pysim's: {bad}")
        return {self.a("compare"): out}



# ---------------------------------------------------------------------------------------------------
# system_xsi and add_system_steps (plans/system_dag.md Stage 4)
# ---------------------------------------------------------------------------------------------------

def snake(name: str) -> str:
    """``MarkovSystem`` -> ``markov_system``."""
    import re

    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


@dataclass(kw_only=True)
class _Inner(BuildStep):
    """One step of ``system_rtl``'s or ``system_xsi``'s inner DAG; *owner* is the outer step holding
    the system, its spec and its workspace.  Never fresh: seconds, and the run after them re-runs
    anyway."""

    params: ClassVar[dict] = {}
    owner: Any

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False


@dataclass(kw_only=True)
class XbarIpStep(_Inner):
    """The crossbar IP (``axi_xbar.generate_axi_xbar``: Vivado ``create_ip``, cached by its config
    digest under ``<work_dir>/ip``)."""

    description = "The crossbar IP, generated from the pysim crossbar (cached by its config digest)."
    produces: ClassVar[dict] = {"xbar_ip": None}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from waveflow.build.axi_xbar import generate_axi_xbar

        return {"xbar_ip": generate_axi_xbar(self.owner.spec.xbar, self.owner.ip_dir(config))}


@dataclass(kw_only=True)
class SystemTopStep(_Inner):
    """The Verilog system top (``system_top.render_system_top``), with the timing probes if any,
    written to ``<work>/<top>.v``."""

    description = "The Verilog system top, walked from the pysim system."
    produces: ClassVar[dict] = {"top_v": None}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from waveflow.build.system_top import render_system_top

        o = self.owner
        out = o.work_dir(config) / f"{o.spec.top}.v"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_system_top(o.spec, o.probes), encoding="utf-8")
        return {"top_v": out}


@dataclass(kw_only=True)
class SystemRtlStep(_SystemStep):
    """``system_rtl``: everything that turns the system into Verilog after csynth -- an inner DAG of
    the crossbar IP and the system top.  Consumes every csynth'd top's RTL (the cut's modules,
    ``system_top_spec(...).modules``); produces ``<work>/<top>.v`` and ``<work>/rtl.json``, the
    manifest of every Verilog file the top needs (the IP's simulation files, the framework leaves its
    instances use -- ``SystemTopSpec.leaf_sources`` -- each csynth'd module's files, the top) and the IP's include directories -- what ``system_xsi`` compiles.
    ``--through system_rtl`` builds all of a system's RTL without simulating it.  The crossbar IP cache
    is ``ip/`` beside the workspace."""

    description = "The system's RTL after csynth: the crossbar IP and the Verilog system top."
    step_name: ClassVar[str] = "system_rtl"

    top: str
    xbar_name: str | None = None
    inside: Any = None
    probes: dict | None = None

    def __post_init__(self) -> None:
        from waveflow.build.system_top import system_top_spec
        from waveflow.build.system_xsi import discover

        super().__post_init__()
        self.xbar, self.host, cut = discover(self.sysm)
        self.inside = list(self.inside) if self.inside is not None else cut
        self.spec = system_top_spec(self.xbar, self.inside, top=self.top, xbar_name=self.xbar_name)

    @property
    def tops(self) -> tuple[str, ...]:
        """The HLS tops this system instantiates: the cut's kernels and the bus writers it brings."""
        return tuple(self.spec.modules)

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [rtl_artifact(t) for t in self.tops]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("rtl"): self.work / "rtl.json"}

    def ip_dir(self, config: BuildConfig) -> Path:
        return self.work_dir(config).parent / "ip"

    def inner_dag(self) -> BuildDag:
        """The inner DAG: the crossbar IP, then the system top."""
        dag = BuildDag()
        dag.add(XbarIpStep(name="xbar_ip", owner=self))
        dag.add(SystemTopStep(name="system_top", owner=self))
        return dag

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import json

        inner = BuildConfig(root_dir=config.root_dir, vitis_version=config.vitis_version,
                            params=dict(config.params))
        dag = self.inner_dag()
        results = dag.run(inner)
        bad = [f"{n}: {r.message}" for n, r in results.items() if not r.success]
        if bad:
            raise RuntimeError("; ".join(bad))
        ip = results["xbar_ip"].artifacts["xbar_ip"]
        root = Path(config.root_dir)
        rtl = [f for m in self.spec.modules for f in sorted((root / rtl_rel(m)).glob("*.v"))]
        manifest = self.work_dir(config) / "rtl.json"
        manifest.write_text(json.dumps({
            "top": self.spec.top,
            # The top last, by its bare name: the workspace compiles it from beside the .f.
            "rtl_files": [Path(f).as_posix()
                          for f in [*ip.sim_files, *self.spec.leaf_sources(), *rtl]]
                         + [f"{self.spec.top}.v"],
            "include_dirs": [Path(d).as_posix() for d in ip.include_dirs],
        }, indent=1) + "\n", encoding="utf-8")
        return {self.a("rtl"): manifest}


@dataclass(kw_only=True)
class HarnessStep(_Inner):
    """The host harness: the testbench ``main`` around the host's C++ twin and its generated endpoints
    (``system_top.render_system_tb``), pointed at the scenario and the RTL trace directory."""

    description = "The testbench: the host's C++ twin on its generated endpoints."
    consumes: ClassVar[list] = ["scenario"]
    produces: ClassVar[dict] = {"harness": None}

    def run(self, config: BuildConfig, scenario, **_: Any) -> dict[str, Any]:
        from waveflow.build.system_top import render_system_tb, system_tb_spec

        o, r = self.owner, self.owner.rtl
        r.host.scenario, r.host.trace_dir = Path(scenario).as_posix(), o.traces(config).as_posix()
        tb = system_tb_spec(r.spec, r.xbar, [r.host], probes=list(r.probes or ()))
        return {"harness": render_system_tb(r.spec, tb)}


@dataclass(kw_only=True)
class XsiRunStep(_Inner):
    """Prepare the XSI workspace (the RTL file list from ``rtl.json``, the testbench) and run it; the
    host's report, parsed, to ``<work>/report.json``."""

    description = "Compile, elaborate and run under XSI; the host's report to report.json."
    consumes: ClassVar[list] = ["rtl", "harness", "scenario"]
    produces: ClassVar[dict] = {"traces": None, "report": None}

    def run(self, config: BuildConfig, rtl, harness, scenario, **_: Any) -> dict[str, Any]:
        import json
        import shutil

        from waveflow.build.system_xsi import XsiRun, parse_output, write_report
        from waveflow.build.xsi_workspace import XsiWorkspace
        from waveflow.toolchain.toolchain import find_vitis_include_dir

        o, root = self.owner, Path(config.root_dir)
        m = json.loads(Path(rtl).read_text(encoding="utf-8"))
        ws = XsiWorkspace(o.work_dir(config), top=m["top"])
        ws.work_dir.mkdir(parents=True, exist_ok=True)
        traces = o.traces(config)
        shutil.rmtree(traces, ignore_errors=True)        # a stale trace would describe another run
        main, tb_files = harness
        tb_inc = [d for d in (find_vitis_include_dir(), root / "include")
                  if d is not None and Path(d).is_dir()]
        ws.prepare(rtl_files=m["rtl_files"], include_dirs=m["include_dirs"],
                   tb_name=f"{m['top']}_tb", tb_cpp=main, extra_files=dict(tb_files),
                   tb_include_dirs=tb_inc)
        out = ws.run(timeout=o.timeout)
        run = XsiRun(output=out, workspace=ws.work_dir, scenario=Path(scenario), traces=traces)
        parse_output(out, run)
        report = write_report(run, ws.work_dir / "report.json")
        return {"traces": traces, "report": report}


@dataclass(kw_only=True)
class SystemXsiStep(_SystemStep):
    """``system_xsi``: the system at RTL under XSI -- an inner DAG of the host harness and the XSI
    run.  Consumes the scenario and ``system_rtl``'s manifest (*rtl*, the :class:`SystemRtlStep`);
    produces ``<work>/traces/`` and ``<work>/report.json`` (the
    :class:`~waveflow.build.system_xsi.XsiRun` fields; :func:`~waveflow.build.system_xsi.load_run`
    reads them back)."""

    description = "The system at RTL under XSI: the host harness, compiled with the RTL, and run."
    step_name: ClassVar[str] = "system_xsi"

    rtl: SystemRtlStep
    timeout: int = 3600

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("scenario"), self.a("rtl")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("traces"): self.work / "traces", self.a("report"): self.work / "report.json"}

    def traces(self, config: BuildConfig) -> Path:
        return self.work_dir(config) / "traces"

    def inner_dag(self, scenario: Path, rtl: Path) -> BuildDag:
        """The inner DAG: the scenario and the RTL manifest as sources, then the harness and the run."""
        dag = BuildDag()
        dag.add(SourceStep(artifact="scenario", path=Path(scenario)))
        dag.add(SourceStep(artifact="rtl", path=Path(rtl)))
        dag.add(HarnessStep(name="harness", owner=self))
        dag.add(XsiRunStep(name="xsi_run", owner=self))
        return dag

    def run(self, config: BuildConfig, **inputs: Any) -> dict[str, Any]:
        inner = BuildConfig(root_dir=config.root_dir, vitis_version=config.vitis_version,
                            params=dict(config.params))
        dag = self.inner_dag(Path(inputs[self.a("scenario")]), Path(inputs[self.a("rtl")]))
        results = dag.run(inner)
        bad = [f"{n}: {r.message}" for n, r in results.items() if not r.success]
        if bad:
            raise RuntimeError("; ".join(bad))
        return {self.a("traces"): self.traces(config),
                self.a("report"): self.work_dir(config) / "report.json"}


def owner_label(obj) -> str:
    """``"u (MmCreditStreamIF)"``: how a report names the pysim object an instance is accounted to."""
    return f"{getattr(obj, 'name', '?')} ({type(obj).__name__})" if obj is not None else ""


@dataclass(kw_only=True)
class KernelResourcesStep(_SystemStep):
    """``kernel_resources``: every Vitis kernel of the system, its csynth report attributed to its
    modules (:func:`~waveflow.build.resource_steps.inspect_synth`) and filed as ``hls_estimate``
    records into the build's platform -- each record carrying the pysim object it is accounted to
    (``owner``: the kernel, or the routed channel that brought a bus writer).  Seconds: it reads
    reports csynth already wrote.  Per top, the attribution goes to
    ``<work>/kernel_resources/<top>.json``; the index, ``<work>/kernel_resources.json``, maps each
    kernel instance to its top, owner and module records."""

    description = "Each kernel's csynth report attributed to its modules; hls_estimate records filed."
    step_name: ClassVar[str] = "kernel_resources"

    rtl: "SystemRtlStep"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [a for t in self.rtl.tops for a in (report_artifact(t), seconds_artifact(t))]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("kernel_resources"): self.work / "kernel_resources.json"}

    def run(self, config: BuildConfig, **inputs: Any) -> dict[str, Any]:
        import json

        from waveflow.build.resource_steps import inspect_synth, synth_seconds_of

        work = self.work_dir(config)
        index: dict[str, dict] = {}
        done: dict[str, dict] = {}
        plat = getattr(config, "platform_info", None)
        filed_into = str(getattr(plat, "write_dir", plat.dir)) if plat is not None else None
        for k in self.rtl.spec.kernels:
            if k.module not in done:              # one csynth per module: attribute it once
                out = work / "kernel_resources" / f"{k.module}.json"
                xml = Path(inputs[report_artifact(k.module)]) / "syn" / "report" / "csynth.xml"
                prev = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
                # Records are filed once per csynth, not once per build: an attribution newer than
                # its report, already filed into this platform, is reused as is.
                if (prev is not None and prev.get("filed_into") == filed_into and xml.is_file()
                        and out.stat().st_mtime >= xml.stat().st_mtime):
                    done[k.module] = prev
                else:
                    done[k.module] = inspect_synth(
                        k.hw, inputs[report_artifact(k.module)], top_name=k.module, config=config,
                        out=out,
                        synth_seconds=synth_seconds_of(inputs.get(seconds_artifact(k.module))),
                        owner=owner_label(k.owner))
            blob = done[k.module]
            index[k.inst] = {"top": k.module, "owner": owner_label(k.owner),
                             "modules": [m["key"] for m in blob["modules"]],
                             "top_total": blob["top"], "integration": blob["integration"],
                             "filed_into": blob["filed_into"]}
        out = work / "kernel_resources.json"
        out.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        return {self.a("kernel_resources"): out}


@dataclass(kw_only=True)
class SystemSynthStep(_SystemStep):
    """``system_synth``: the system top through Vivado, out of context -- every instance of the
    inventory attributed from ``report_utilization -hierarchical``, the integration term and the
    total (:func:`~waveflow.build.system_synth.synth_system`).  Minutes, so **opt-in**: no default
    target depends on it (``--through system_synth``).  Cached by the system's identity, so a re-run
    with nothing changed starts no Vivado.  Synthesized for the part and clock the kernels were
    csynth'd for (:func:`~waveflow.build.composite_gen.tcl_target`: the build's platform, else the
    default); the crossbar IP is generated for that part beside the simulation one.  With a platform
    selected the records are filed into it.  *impl* also places and routes (closure in the records).

    Produces ``<work>/system_synth.json`` (the :class:`~waveflow.build.system_synth.SystemSynth`)."""

    description = "The system top synthesized in Vivado; every instance attributed (opt-in, minutes)."
    step_name: ClassVar[str] = "system_synth"

    rtl: "SystemRtlStep"
    impl: bool = False
    #: ``KEEP_HIERARCHY`` on every inventory instance (synthesis copy of the top only).  Off by
    #: default: it cost markov +25% LUT / +30% FF -- numbers for a design nobody would ship.  An
    #: instance the default flow dissolves is priced by its cells' names instead.
    keep_hierarchy: bool = False

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("rtl")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("system_synth"): self.work / "system_synth.json"}

    def kernel_dirs(self, config: BuildConfig) -> dict[str, Path]:
        root = Path(config.root_dir)
        return {m: root / rtl_rel(m) for m in self.rtl.spec.modules}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import dataclasses
        import json

        from waveflow.build.axi_xbar import generate_axi_xbar
        from waveflow.build.composite_gen import tcl_target
        from waveflow.build.system_synth import synth_system, system_synth_records

        spec, work = self.rtl.spec, self.work_dir(config)
        part, period = tcl_target(config)
        ip = generate_axi_xbar(dataclasses.replace(spec.xbar, part=part), self.rtl.ip_dir(config))
        top_v = work / f"{spec.top}.v"
        kdirs = self.kernel_dirs(config)
        res = synth_system(spec, top_v=top_v, kernel_dirs=kdirs, ip=ip,
                           out_dir=work.parent / "system_synth", part=part, period_ns=period,
                           impl=self.impl, keep_hierarchy=self.keep_hierarchy)
        blob = res.to_json()
        plat = getattr(config, "platform_info", None)
        blob["filed_into"] = None
        if plat is not None:
            from waveflow.build.system_synth import INSTANCE_TARGET, system_identity
            from waveflow.calib.record_store import platform_store

            store = platform_store(plat)
            top_text = top_v.read_text(encoding="utf-8")
            ident, _run = system_identity(spec, top_text, kdirs, part=part, period_ns=period,
                                          impl=self.impl)
            # A cached run's records are already filed: file once per synthesis, not per build.
            filed = any(r.payload.get("kind") == "system" and r.provenance.part == part
                        and r.payload.get("keep_hierarchy", False) == self.keep_hierarchy
                        for r in store.read(ident.key, INSTANCE_TARGET))
            if not filed:
                for rec, i_ident in system_synth_records(spec, res, top_text, kdirs,
                                                         owner_label=owner_label):
                    store.append(rec, identity=i_ident)
            blob["filed_into"] = str(getattr(plat, "write_dir", plat.dir))
        out = work / "system_synth.json"
        out.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
        i = res.integration
        print(f"  {spec.top}: {len(res.instances)} instances + integration {i} = total {res.total}")
        return {self.a("system_synth"): out}


@dataclass(kw_only=True)
class LeafResourcesStep(_SystemStep):
    """``leaf_resources``: every hand-written leaf and IP configuration of the system, looked up in the
    platform's store by identity; with the param ``measure_leaves`` true, each missing configuration
    synthesized out of context once and filed (:func:`~waveflow.build.rtl_ooc.measure_instances`).
    By default a miss is only reported -- the estimate shows it ``UNCALIBRATED`` -- so the step is
    seconds unless asked to spend.  Produces ``<work>/leaf_resources.json``: per instance, its key and
    ``stored`` / ``measured`` / ``missing`` / ``failed``."""

    description = "Each leaf / IP configuration: in the store, or (measure_leaves) synthesized OOC once."
    step_name: ClassVar[str] = "leaf_resources"
    params: ClassVar[dict] = {"measure_leaves": False}

    rtl: "SystemRtlStep"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("rtl")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("leaf_resources"): self.work / "leaf_resources.json"}

    def run(self, config: BuildConfig, measure_leaves: bool = False, **_: Any) -> dict[str, Any]:
        import json

        from waveflow.build.composite_gen import tcl_target
        from waveflow.build.rtl_ooc import measure_instances

        plat = getattr(config, "platform_info", None)
        if measure_leaves and plat is None:
            raise RuntimeError("leaf_resources: measure_leaves needs a platform to file into "
                               "(--platform)")
        from waveflow.calib.record_store import platform_store

        part, period = tcl_target(config)
        work = self.work_dir(config)
        status = measure_instances(self.rtl.spec, platform_store(plat) if plat else None,
                                   part=part, period_ns=period, out_dir=work.parent / "rtl_ooc",
                                   ip_dir=self.rtl.ip_dir(config), measure=bool(measure_leaves))
        out = work / "leaf_resources.json"
        out.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        counts: dict[str, int] = {}
        for e in status.values():
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        print(f"  {self.rtl.spec.top}: leaf / IP instances {counts}")
        failed = {k: e["error"] for k, e in status.items() if e["status"] == "failed"}
        if failed:
            raise RuntimeError(f"out-of-context synthesis failed for {sorted(failed)}: "
                               f"{next(iter(failed.values()))[-1500:]}")
        return {self.a("leaf_resources"): out}


@dataclass(kw_only=True)
class ResourceReportStep(_SystemStep):
    """``resource_report``: the system's estimate (:func:`~waveflow.build.system_estimate.estimate_system`)
    -- a row per pysim object, each with its counters, confidence and where its number came from, the
    system integration row, and, when ``system_synth`` has filed a measured total, that total beside
    the prediction with the gap.  Reads the build's platform; consumes ``kernel_resources`` and
    ``leaf_resources`` so their records are in the store first.  ``system_synth`` is not consumed (it
    is opt-in): run it first and the report finds its records.  Produces ``<work>/resource_report.json``
    and the table as ``resource_report.txt``."""

    description = "The system estimate: a row per pysim object, the measured total beside it."
    step_name: ClassVar[str] = "resource_report"

    rtl: "SystemRtlStep"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("kernel_resources"), self.a("leaf_resources")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("resource_report"): self.work / "resource_report.json"}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import json

        from waveflow.build.system_estimate import estimate_system

        plat = getattr(config, "platform_info", None)
        est = estimate_system(self.sysm, plat, spec=self.rtl.spec, root=config.root_dir)
        work = self.work_dir(config)
        out = work / "resource_report.json"
        out.write_text(json.dumps(est.to_json(), indent=2) + "\n", encoding="utf-8")
        table = est.format_table()
        (work / "resource_report.txt").write_text(table + "\n", encoding="utf-8")
        print(table if plat is not None else
              table + "\n  (no platform selected: nothing to look up -- pass --platform)")
        return {self.a("resource_report"): out}


def add_system_steps(dag: BuildDag, sysm, *, work_dir, top: str | None = None,
                     xbar_name: str | None = None, inside=None, probes: dict | None = None,
                     prefix: str = "", workspace: str | None = None,
                     sources: tuple[str, ...] = ("include", "gen"),
                     tcls: dict[str, Path] | None = None, timeout: int = 3600) -> SystemXsiStep:
    """Add the framework steps of a system's flow to *dag*: ``csynth`` (one inner step per HLS top in
    the cut), ``system_rtl``, ``kernel_resources``, ``system_synth``, ``leaf_resources``,
    ``resource_report``, ``scenario``, ``pysim``, ``system_xsi`` and ``compare``.  Returns the
    ``system_xsi`` step (its ``rtl`` is the ``system_rtl`` step).

    *sysm* is the pysim system object, not yet run.  csynth consumes the artifacts *sources* -- the
    example's ``codegen`` products; one the DAG has no producer for becomes a source directory under
    the root.  *top* names the Verilog top (default: the system class in snake case, ``MarkovSystem``
    -> ``markov_system``), *xbar_name* the crossbar IP (default ``xbar_<top>``).  *inside* overrides
    the cut; *probes* (``{name: beat(...)}``) adds timing probes; *tcls* overrides a top's csynth
    script (``{top: path relative to the root}``).

    The run lives in ``<work_dir>/<workspace>`` (default ``<prefix><top>``, ``_probes`` added with
    probes) and the crossbar IP cache in ``<work_dir>/ip``; a relative *work_dir* is under the root.
    *prefix* names a second system in the same DAG: its steps and artifacts are prefixed, and a top an
    earlier system's csynth already builds is consumed from it, not built twice -- so two topologies
    of one kernel share one ``codegen`` and one ``csynth``.  The first csynth is named ``csynth``; one
    a later system needs for tops of its own is ``<prefix>csynth``.
    """
    top = top or snake(type(sysm).__name__)
    work_dir = Path(work_dir)
    work = work_dir / ((workspace or f"{prefix}{top}") + ("_probes" if probes else ""))
    rtl = SystemRtlStep(sysm=sysm, work=work, prefix=prefix, top=top, xbar_name=xbar_name,
                        inside=inside, probes=probes)
    owners = dag.artifact_owners()
    for name in sources:
        if name not in owners:
            dag.add(SourceStep(artifact=name, path=Path(name)))
    new = [t for t in rtl.tops if rtl_artifact(t) not in owners]
    if new:
        name = "csynth" if "csynth" not in dag.step_names() else prefix + "csynth"
        dag.add(CsynthTopsStep(name=name, tops=new, tcls=dict(tcls or {}), sources=tuple(sources)))
    dag.add(rtl)
    dag.add(KernelResourcesStep(sysm=sysm, work=work, prefix=prefix, rtl=rtl))
    dag.add(SystemSynthStep(sysm=sysm, work=work, prefix=prefix, rtl=rtl))
    dag.add(LeafResourcesStep(sysm=sysm, work=work, prefix=prefix, rtl=rtl))
    dag.add(ResourceReportStep(sysm=sysm, work=work, prefix=prefix, rtl=rtl))
    dag.add(ScenarioStep(sysm=sysm, work=work, prefix=prefix))
    dag.add(PysimStep(sysm=sysm, work=work, prefix=prefix))
    xsi = SystemXsiStep(sysm=sysm, work=work, prefix=prefix, rtl=rtl, timeout=timeout)
    dag.add(xsi)
    dag.add(CompareStep(sysm=sysm, work=work, prefix=prefix))
    return xsi


__all__ = ["CompareStep", "CsynthStep", "CsynthTopsStep", "KernelResourcesStep", "LeafResourcesStep",
           "PysimStep", "ResourceReportStep", "SystemSynthStep",
           "SYNTH_MODES", "ScenarioStep", "SystemRtlStep", "SystemXsiStep", "add_system_steps",
           "owner_label", "report_artifact", "report_rel", "rtl_artifact", "rtl_problem", "rtl_rel",
           "seconds_artifact", "snake"]
