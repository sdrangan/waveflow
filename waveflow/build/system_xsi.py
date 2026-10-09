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
    """``(xbar, host, inside)`` of a system object: its one crossbar, its one software host, and the
    default cut -- each kernel whose device is a crossbar slave, and each memory on the crossbar."""
    from waveflow.hw.memif import AXIMMCrossBarIF
    from waveflow.hw.memory import MemoryMod
    from waveflow.sw import SwHost

    objs = list(getattr(sysm.sim, "_sim_objs", []))
    xbars = [o for o in objs if isinstance(o, AXIMMCrossBarIF)]
    hosts = [o for o in objs if isinstance(o, SwHost)]
    if len(xbars) != 1 or len(hosts) != 1:
        raise LoweringError(f"{type(sysm).__name__}: a bus system has one crossbar and one software host; "
                            f"found {len(xbars)} crossbar(s) and {len(hosts)} host(s)")
    xbar, host = xbars[0], hosts[0]
    inside: list = []
    for k in range(int(xbar.nports_slave)):
        ep = xbar.endpoints.get(f"slave_{k}")
        mod = getattr(ep, "comp", None)
        dev = getattr(mod, "mm_device", None)
        target = dev.kernel if dev is not None else (mod if isinstance(mod, MemoryMod) else None)
        if target is not None and all(target is not m for m in inside):
            inside.append(target)
    return xbar, host, inside


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



def compare_traces(a: Path, b: Path) -> list[str]:
    """``"<endpoint>/<file>"`` for every trace file under *a* that is missing or different under *b*
    (and every endpoint *b* has that *a* lacks)."""
    bad = []
    names = sorted({p.name for p in Path(a).iterdir()} | {p.name for p in Path(b).iterdir()})
    for ep in names:
        for f in ("words.bin", "bounds.bin", "meta.json"):
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
