"""mm_fir_build.py — the mm_fir example's build DAG: generate and synthesize the kernel (rung 3 of
plans/mm_slave_adaptor.md, ``plans/system_dag.md``).

    python -m examples.mm_fir.mm_fir_build                                # everything, both topologies
    python -m examples.mm_fir.mm_fir_build --through one_front_compare    # one topology
    python -m examples.mm_fir.mm_fir_build --through per_view_pysim       # pysim only (no Vivado)
    python -m examples.mm_fir.mm_fir_build --through csynth               # build the RTL, stop there
    python -m examples.mm_fir.mm_fir_build --status                       # what is stale, and why

The kernel's body is hand-written (``include/mm_fir_task.h``, the HLS twin of ``MmFir.run_iter``);
everything around it is generated: the config / status structs from :class:`FirCfg` /
:class:`FirStatus` (so neither side hand-packs a word), and the free-running ``ap_ctrl_none`` top from
the module's own ports via :func:`~waveflow.build.composite_gen.composite_top_spec`.  The memory-mapped
side is not in this top at all -- the adaptor is RTL beside it, in the system top.

``codegen`` and the scenario (:func:`system`) are this example's; the rest is the framework's
(:func:`~waveflow.build.system_dag.add_system_steps`), added once per topology -- one address map (view
*k* at ``REGS + k * 4 KB``), two buses:

* ``per_view``  -- a 1x4 crossbar; each view its own MI slot and its own front;
* ``one_front`` -- all four views behind ONE front and a generated decoder, on MI0 of a 1x2 crossbar
  (MI1 is a stub nothing addresses: a 1x1 crossbar is degenerate -- see ``AxiXbarConfig``).

Both share one ``codegen`` and one ``csynth`` (the kernel's RTL does not depend on the bus); each has
its own ``<topology>_scenario`` / ``_pysim`` / ``_system_xsi`` / ``_compare``, running in
``xsi_work/mm_fir_<topology>/``.  The gates are ``tests/examples/test_mm_fir_xsi.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl, render_top
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.simulation.simulation import Simulation

import numpy as np

from examples.mm_fir.mm_fir import (
    DW,
    S16,
    S64,
    FirCfg,
    FirCmdHdr,
    FirRespHdr,
    FirStatus,
    MmFir,
    MmFirSystem,
    Taps,
    timing_probes,
)

HERE = Path(__file__).resolve().parent
TOP = "mm_fir"

#: The two topologies' crossbar IP names (kept from before the DAG, so the IP cache holds), the system
#: top, and where the runs go (``<work_dir>/mm_fir_<topology>``).
XBAR_NAMES = {"per_view": "xbar_mm4_1x4", "one_front": "xbar_mm1_1x2"}
SYSTEM_TOP, WORK_DIR = "mm_fir_top", "xsi_work"

#: The scenario: 200 samples in packets of 16, the config switching from TAPS_A to TAPS_B at 101.
NSAMP, SWITCH_AT, PKT = 200, 101, 16
TAPS_A = [3, -1, 4, 1, -5]
TAPS_B = [2, 7, 1, -8, 2, 8, 1, -8]
PLAN = [(0, TAPS_A), (SWITCH_AT, TAPS_B)]


def scenario_x() -> np.ndarray:
    return np.random.default_rng(7).integers(-2000, 2000, size=NSAMP)


def system(topology: str) -> MmFirSystem:
    """The pysim system for *topology* on the scenario -- what the DAG runs in pysim and at RTL."""
    if topology not in XBAR_NAMES:
        raise ValueError(f"topology must be one of {sorted(XBAR_NAMES)}, got {topology!r}")
    return MmFirSystem(x=list(scenario_x()), plan=PLAN, pkt=PKT, one_front=topology == "one_front")


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))     # the generated top includes memmgr.hpp
    for cls in (Taps, FirCmdHdr, FirRespHdr, FirCfg, FirStatus):
        dag.add(DataSchemaStep(cls, word_bw_supported=[DW], include_dir=INCLUDE_DIR))
    # The lane routines the body packs samples and results with: int16 four to a word, int64 one.
    for elem in (S16, S64):
        dag.add(ArrayUtilsStep(elem, [DW]))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(root: Path = HERE) -> Path:
    leaf = MmFir(name=TOP, sim=Simulation())
    spec = composite_top_spec(leaf, width=DW)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (root / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    return cpp


def generate(root: Path = HERE) -> list[str]:
    """Everything before csynth -- the headers, the top and its ``.tcl`` -- and the name of the one top.
    Python only, seconds, no toolchain."""
    gen_headers(root)
    return [gen_top(root).stem]


@dataclass(kw_only=True)
class MmFirCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh: its inputs are Python and framework headers the DAG
    cannot see, and the rewrite is cheap; csynth after it decides by content."""

    description = "Generate the headers, the kernel top and its .tcl."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(probes: bool = False, work_dir=WORK_DIR) -> BuildDag:
    """``codegen``, then the framework's system steps once per topology (prefix ``<topology>_``),
    sharing one ``csynth`` (the one top, its ``.tcl`` at the example root).  *probes* builds each system
    top with :func:`~examples.mm_fir.mm_fir.timing_probes` (workspaces ``mm_fir_<topology>_probes``)."""
    dag = BuildDag()
    dag.add(MmFirCodegenStep(name="codegen"))
    for topology, xbar_name in XBAR_NAMES.items():
        sysm = system(topology)
        add_system_steps(dag, sysm, work_dir=work_dir, top=SYSTEM_TOP, xbar_name=xbar_name,
                         prefix=f"{topology}_", workspace=f"mm_fir_{topology}",
                         tcls={TOP: Path(f"{TOP}.tcl")},
                         probes=timing_probes(sysm) if probes else None)
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(probes=a.probes), description=__doc__.splitlines()[0],
                default_through=None, root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            (("--probes",), dict(action="store_true",
                             help="build the system tops with the timing probes"))],
                params_from_args=lambda a: {"synth": a.synth})
