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

The result is an :class:`XsiRun`: the raw output, the parsed ``DONE`` numbers, the bus operations, the
paths of the workspace, the scenario and both sets of traces, and the trace mismatches (empty: pass).
"""
from __future__ import annotations

import inspect
import re
import shutil
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


def _parse(out: str, run: XsiRun) -> None:
    m = re.search(r"^DONE done=(\d+) cycles=(-?\d+) polls=(-?\d+) nops=(\d+)", out, re.M)
    if m:
        run.done, run.cycles, run.polls, run.nops = bool(int(m[1])), int(m[2]), int(m[3]), int(m[4])
    run.ops = [(k == "W", int(a, 16), int(n), int(s), int(e)) for k, a, n, s, e in
               re.findall(r"^OP ([RW]) 0x([0-9a-f]+) n=(\d+) s=(-?\d+) e=(-?\d+)", out, re.M)]


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
    host check (and leaves *sysm* un-run)."""
    from waveflow.build.axi_xbar import generate_axi_xbar
    from waveflow.build.mm_adaptor_gen import leaf_sources
    from waveflow.build.system_top import (
        render_system_tb,
        render_system_top,
        system_tb_spec,
        system_top_spec,
    )
    from waveflow.build.trace_steps import rtl_staleness
    from waveflow.build.xsi_workspace import XsiWorkspace
    from waveflow.hw.memory import MemoryMod
    from waveflow.toolchain.toolchain import find_vitis_include_dir

    xbar, host, cut = discover(sysm)
    inside = list(inside) if inside is not None else cut
    kernels = [m for m in inside if not isinstance(m, MemoryMod)]
    if root is None:
        if not kernels:
            raise LoweringError(f"{type(sysm).__name__}: no kernel inside the cut")
        root = Path(inspect.getfile(type(kernels[0]))).resolve().parent
    root = Path(root)
    spec = system_top_spec(xbar, inside, top=top, xbar_name=xbar_name)

    # 2. The RTL: present, and built from these sources.
    for module in spec.modules:
        d = _rtl_dir(root, module)
        if not d.is_dir():
            raise FileNotFoundError(f"no csynth RTL for {module} at {d}: build the example first "
                                    f"(its *_build script)")
        stale = rtl_staleness(root, module)
        if stale is not None:
            raise RuntimeError(f"{module}: the RTL on disk was not built from these sources -- {stale}")

    # 3. Generate.
    work_dir = Path(work_dir).resolve()
    ip = generate_axi_xbar(spec.xbar, work_dir / "ip")
    ws = XsiWorkspace(work_dir / ((workspace or top) + ("_probes" if probes else "")), top=spec.top)
    ws.work_dir.mkdir(parents=True, exist_ok=True)
    scenario, traces = ws.work_dir / "scenario", ws.work_dir / "traces"
    host.scenario, host.trace_dir = scenario.as_posix(), traces.as_posix()
    host.write_scenario(scenario)
    shutil.rmtree(traces, ignore_errors=True)            # a stale trace would describe another run
    tb = system_tb_spec(spec, xbar, [host], probes=list(probes or ()))
    main, tb_files = render_system_tb(spec, tb)
    rtl = [f for m in spec.modules for f in sorted(_rtl_dir(root, m).glob("*.v"))]
    tb_inc = [d for d in (find_vitis_include_dir(), root / "include") if d is not None and Path(d).is_dir()]
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + rtl + [f"{spec.top}.v"],
               include_dirs=ip.include_dirs, tb_name=f"{spec.top}_tb", tb_cpp=main,
               extra_files={f"{spec.top}.v": render_system_top(spec, probes), **tb_files},
               tb_include_dirs=tb_inc)

    # 4. Run.
    out = ws.run(timeout=timeout)
    run = XsiRun(output=out, workspace=ws.work_dir, scenario=scenario, traces=traces)
    _parse(out, run)

    # 5. The host check: the same system in pysim, from the same scenario file.
    if compare_pysim:
        run.pysim_traces = ws.work_dir / "pysim_traces"
        shutil.rmtree(run.pysim_traces, ignore_errors=True)
        host.trace_dir = run.pysim_traces.as_posix()
        sysm.run()
        run.pysim_cycles = sysm.sim.env.now / host.clk.period
        run.trace_mismatches = compare_traces(traces, run.pysim_traces)
    return run


__all__ = ["XsiRun", "compare_traces", "discover", "run_system_xsi"]
