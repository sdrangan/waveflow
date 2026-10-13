"""system_xsi.py — run a whole system at RTL from its pysim object: ``run_system_xsi(sysm, ...)``.

``plans/host_runtime.md`` Stage 5.  The system object -- the same one ``sysm.run()`` simulates in pysim
-- is all it needs:

1. **discover** the crossbar and the software host among the simulation's objects, and the default
   cut: every kernel whose memory-mapped device sits on the crossbar, and every memory on it;
2. **check the RTL** of every module the top instantiates is there and was built from the sources on
   disk (``rtl_staleness``) -- building it stays the example's ``*_build`` script, named in the error;
3. **generate** the crossbar IP, the top (``system_top``), the harness with the host's C++ twin and its
   generated endpoints, and the scenario the host writes;
4. **run** it under XSI, and parse the host's report;
5. **check the host** (optional, default on): run the same system in pysim from the same scenario and
   compare every host endpoint's trace, file for file -- the conformance gate of
   ``plans/xsi_system_top.md``.

Since ``plans/system_dag.md`` each of those is a step of a ``BuildDag``
(:func:`waveflow.build.system_dag.add_system_steps`: ``csynth`` in check mode, ``system_rtl``,
``scenario``, ``pysim``, ``system_xsi``, ``compare``), and :func:`run_system_xsi` is a thin wrapper that builds that
DAG, runs it and reads the result back (:func:`load_run`).  An example's own build DAG runs the same
steps, with ``csynth`` allowed to build.

The result is an :class:`XsiRun`: the raw output, the parsed ``DONE`` numbers, the bus operations, the
paths of the workspace, the scenario and both sets of traces, and the trace mismatches (empty: pass).
"""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from waveflow.build.hwcodegen import LoweringError


@dataclass
class XsiRun:
    """One RTL run of a system."""

    output: str
    workspace: Path
    scenario: Path
    traces: Path
    done: bool = False
    cycles: int = -1
    polls: int = -1
    nops: int = -1
    #: Every bus operation the host issued: ``(write, addr, nwords, t_start, t_end)``.
    ops: list = field(default_factory=list)
    #: The pysim run's traces, when the host was checked (``compare_pysim``).
    pysim_traces: Path | None = None
    pysim_cycles: float | None = None
    #: ``"<endpoint>/<file>"`` for every trace file that differs from pysim's; empty when they agree.
    trace_mismatches: list = field(default_factory=list)


def discover(sysm):
    """``(xbar, host, inside)`` of a :class:`~waveflow.hw.hw_system.HwSystem`: its bus, the
    testbench participant that drives it, and its default cut
    (:meth:`~waveflow.hw.hw_system.HwSystem.cut`).

    Each is found by **role**, not by type (``plans/system_model.md`` D1):

    * the **bus** is the DUT's link that realizes as bus IP -- the interface of the DUT tree that
      declares an ``rtl_config`` (S2), today the crossbar -- or ``None``: a system needs no bus;
    * the **host** is the participant with a bus master on that bus whose owner is **outside** the
      DUT tree -- the testbench side of the cut -- or ``None``.  Its realization is its own
      ``bfm_model()``.

    At most one of each, today (``plans/system_model.md`` S3)."""
    from waveflow.hw.hw_system import HwSystem

    if not isinstance(sysm, HwSystem):
        raise LoweringError(f"{type(sysm).__name__} is not a waveflow HwSystem: a system flow takes a "
                            f"subclass of waveflow.hw.hw_system.HwSystem, which builds its graph on "
                            f"self.sim and implements dut() (the default cut) and run()")
    root = sysm.dut()
    buses = [i for i in root.interfaces.values() if callable(getattr(i, "rtl_config", None))]
    if len(buses) > 1:
        raise LoweringError(f"{type(sysm).__name__}: a system top links its devices through at most one "
                            f"crossbar (an interface realized as bus IP, rtl_config); its DUT "
                            f"'{root.name}' has {len(buses)}")
    if not buses:                       # no bus: the testbench reaches the DUT through its own ports
        return None, None, sysm.cut()
    bus = buses[0]

    def in_dut(mod) -> bool:
        while mod is not None:
            if mod is root:
                return True
            mod = getattr(mod, "parent", None)
        return False

    hosts: list = []
    for name, ep in bus.endpoints.items():
        owner = getattr(ep, "comp", None)
        if name.startswith("master_") and owner is not None and not in_dut(owner) \
                and all(owner is not h for h in hosts):
            hosts.append(owner)
    if len(hosts) > 1:
        raise LoweringError(f"{type(sysm).__name__}: a system top is driven by at most one testbench "
                            f"bus master (a host outside the DUT on '{bus.name}'); found "
                            f"{[getattr(h, 'name', h) for h in hosts]}")
    return bus, (hosts[0] if hosts else None), sysm.cut()


def elaborate_sub_top(spec, name: str, *, root: Path, work_dir: Path) -> Path:
    """Compile device (or memory) *name* of *spec* **alone** and elaborate it: the standalone module
    (:meth:`~waveflow.build.system_top.SystemTopSpec.render_sub_verilog`), its leaves, and its
    kernels' csynth Verilog under *root* -- nothing else.  It passing is what makes a sub-top a unit:
    a per-device synthesis or IP can be built from it with no system around it
    (``plans/system_model.md`` S5).  Returns the work directory; raises
    :class:`~waveflow.scripts.sv_sim.SvSimError` naming the failing tool."""

    sub = next(s for s in spec.subs if s.name == name)
    leaves, kernels = spec.sources_of(name)
    return _elaborate(sub.module, spec.render_sub_verilog(name), leaves, kernels, root=root,
                      work_dir=work_dir)


def elaborate_top(spec, *, root: Path, work_dir: Path) -> Path:
    """Compile and elaborate a **whole** system top that has no crossbar: its Verilog, every leaf it
    uses and every kernel's csynth Verilog under *root*.  For a top with no bus there is no vendor IP
    to generate, so this is all it needs -- what shows a system with no crossbar (mem_copy) is a real
    design before a harness is wrapped around it (``plans/system_model.md`` S3a).  A top with a
    crossbar needs its IP's simulation files, which the ``system_rtl`` step generates; refused here."""
    from waveflow.build.hwcodegen import LoweringError

    if spec.xbar is not None:
        raise LoweringError(f"{spec.top} has a crossbar: elaborate it through its system_rtl step, "
                            f"which generates the IP's simulation files")
    return _elaborate(spec.top, spec.render_verilog(), spec.leaf_sources(), spec.modules, root=root,
                      work_dir=work_dir)


def _elaborate(module: str, text: str, leaves, kernels, *, root: Path, work_dir: Path) -> Path:
    """xvlog + xelab of *module* (Verilog *text*) with its *leaves* and its *kernels*' csynth
    Verilog under *root*, in ``<work_dir>/<module>``."""
    from waveflow.scripts.sv_sim import _run, sim_tool

    work = Path(work_dir) / module
    work.mkdir(parents=True, exist_ok=True)
    top_v = work / f"{module}.v"
    top_v.write_text(text, encoding="utf-8")
    files = [*leaves]
    for m in kernels:
        files += sorted(_rtl_dir(Path(root), m).glob("*.v"))
    files.append(top_v)
    (work / f"{module}.f").write_text("\n".join(Path(f).resolve().as_posix() for f in files)
                                      + "\n", encoding="utf-8")
    _run(f'"{sim_tool("xvlog")}" -f {module}.f', work, capture_output=True, echo=False)
    _run(f'"{sim_tool("xelab")}" work.{module} -s {module}_elab', work,
         capture_output=True, echo=False)
    return work


def _rtl_dir(root: Path, module: str) -> Path:
    return root / f"{module}_proj" / "solution1" / "syn" / "verilog"


def parse_output(out: str, run: XsiRun) -> None:
    """Fill *run*'s ``DONE`` numbers and bus operations from the host's report *out*."""
    m = re.search(r"^DONE done=(\d+) cycles=(-?\d+) polls=(-?\d+) nops=(\d+)", out, re.M)
    if m:
        run.done, run.cycles, run.polls, run.nops = bool(int(m[1])), int(m[2]), int(m[3]), int(m[4])
    run.ops = [(k == "W", int(a, 16), int(n), int(s), int(e)) for k, a, n, s, e in
               re.findall(r"^OP ([RW]) 0x([0-9a-f]+) n=(\d+) s=(-?\d+) e=(-?\d+)", out, re.M)]


_parse = parse_output          # the name before plans/system_dag.md


def write_report(run: XsiRun, path) -> Path:
    """The RTL half of *run* -- the raw output, the ``DONE`` numbers, the bus operations, the paths --
    as JSON at *path* (``system_xsi``'s ``report.json``).  :func:`load_run` reads it back."""
    path = Path(path)
    path.write_text(json.dumps({
        "output": run.output, "workspace": Path(run.workspace).as_posix(),
        "scenario": Path(run.scenario).as_posix(), "traces": Path(run.traces).as_posix(),
        "done": run.done, "cycles": run.cycles, "polls": run.polls, "nops": run.nops,
        "ops": [list(op) for op in run.ops],
    }, indent=1) + "\n", encoding="utf-8")
    return path


def load_run(work) -> XsiRun:
    """The :class:`XsiRun` a system DAG left in workspace *work*: ``report.json`` (the RTL run), and
    when the host was checked, ``pysim.json`` and ``compare.json``."""
    work = Path(work)
    r = json.loads((work / "report.json").read_text(encoding="utf-8"))
    run = XsiRun(output=r["output"], workspace=Path(r["workspace"]), scenario=Path(r["scenario"]),
                 traces=Path(r["traces"]), done=r["done"], cycles=r["cycles"], polls=r["polls"],
                 nops=r["nops"], ops=[tuple(op) for op in r["ops"]])
    if (work / "pysim.json").is_file():
        run.pysim_traces = work / "pysim_traces"
        run.pysim_cycles = json.loads((work / "pysim.json").read_text(encoding="utf-8"))["cycles"]
    if (work / "compare.json").is_file():
        run.trace_mismatches = json.loads(
            (work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"]
    return run



def compare_traces(a: Path, b: Path,
                   files: tuple[str, ...] = ("words.bin", "bounds.bin", "meta.json"),
                   names=None) -> list[str]:
    """``"<endpoint>/<file>"`` for every trace file under *a* that is missing or different under *b*
    (and every endpoint *b* has that *a* lacks).  *files* chooses what is compared: a host's traces
    whole, a testbench recording on its ``words.bin``.  *names*, when given, limits the comparison
    to those trace directories."""
    bad = []
    present = {p.name for p in Path(a).iterdir()} | {p.name for p in Path(b).iterdir()}
    names = sorted(present if names is None else set(names))
    for ep in names:
        for f in files:
            pa, pb = Path(a) / ep / f, Path(b) / ep / f
            if not (pa.is_file() and pb.is_file() and pa.read_bytes() == pb.read_bytes()):
                bad.append(f"{ep}/{f}")
    return bad


def run_system_xsi(sysm, work_dir, *, top: str, xbar_name: str | None = None, inside=None,
                   root=None, probes: dict | None = None, compare_pysim: bool = True,
                   workspace: str | None = None, timeout: int = 3600) -> XsiRun:
    """Run the system *sysm* -- a pysim system object, not yet run -- at RTL.  See the module docstring.

    *top* names the Verilog top; *xbar_name* the crossbar IP (default ``xbar_<top>``; give the name a
    previous run used to reuse its generated IP).  *inside* overrides the default cut.  *root* is the
    example directory holding the csynth projects (default: the directory of the first kernel's
    module).  *workspace* names the run's directory under *work_dir* (default *top*, ``_probes`` added
    with probes).  *probes* -- ``{name: beat(...)}`` -- adds timing probes.  ``compare_pysim=False`` skips the
    host check (and leaves *sysm* un-run).

    A thin wrapper over the system DAG (:func:`waveflow.build.system_dag.add_system_steps`), run with
    ``synth="check"`` -- it never synthesizes; a missing or stale top fails, naming it -- through
    ``compare`` (``system_xsi`` without the host check), and read back with :func:`load_run`."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.system_dag import add_system_steps
    from waveflow.hw.memory import MemoryMod

    _xbar, _host, cut = discover(sysm)
    if root is None:
        kernels = [m for m in (list(inside) if inside is not None else cut)
                   if not isinstance(m, MemoryMod)]
        if not kernels:
            raise LoweringError(f"{type(sysm).__name__}: no kernel inside the cut")
        root = Path(inspect.getfile(type(kernels[0]))).resolve().parent
    dag = BuildDag()
    xsi = add_system_steps(dag, sysm, work_dir=Path(work_dir).resolve(), top=top,
                           xbar_name=xbar_name, inside=inside, probes=probes,
                           workspace=workspace or top, timeout=timeout)
    config = BuildConfig(root_dir=Path(root), params={"synth": "check"})
    results = dag.run(config, through=xsi.a("compare") if compare_pysim else xsi.name)
    for name, res in results.items():
        if not res.success and name != xsi.a("compare"):     # compare's verdict is the run's data
            raise RuntimeError(f"{name}: {res.message}")
    return load_run(xsi.work_dir(config))


__all__ = ["XsiRun", "compare_traces", "discover", "load_run", "parse_output", "run_system_xsi",
           "write_report"]
