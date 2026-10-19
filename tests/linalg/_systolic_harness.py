"""The systolic unit in its own harness, stream-fed: the DAG that runs it at RTL and calibrates the
core's timed phases (``plans/systolic_characterization.md``, Stage 3).

    harness(unit, workload="test")   -> codegen, csynth, pysim, golden, system_xsi, compare
    add_calib_steps(...)              -> calib_* : the workload("calib") traced, calib_phases

The unit's top is csynth'd once and shared by both systems.  Build from a short root: the task names
make csynth's report paths long, and past Windows' 260 characters csynth fails with no message
(``plans/systolic_characterization_lessons.md``).

    python -m tests.linalg._systolic_harness --root C:/wfsc/sh --through compare
    python -m tests.linalg._systolic_harness --root C:/wfsc/sh --through calib_phases
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl,
                                          render_top, tcl_path)
from waveflow.build.phase_calib import add_calib_steps
from waveflow.build.streamutils import MemMgrStep
from waveflow.build.system_dag import add_system_steps
from waveflow.calib.record_store import ModuleStore
from waveflow.hw.harness import harness
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.linalg.systolic import CALIB_DIR, SystolicUnit
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode

from tests.linalg._hls import PART, PERIOD_NS

#: The system top (not the unit's own name: the wrapper would instantiate itself).
TOP, WORK_DIR = "systolic_unit_sys", "xsi_work"


def reg(W: int, I: int) -> Format:  # noqa: E741 -- (width, integer bits), as Format names them
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


#: The centre configuration of the systolic page.
CENTRE = dict(Mmax=8, Kmax=8, Nmax=32, R=4, C=8, L=4, form=4, word_bits=64,
              a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))


def unit(sim=None, **cfg) -> SystolicUnit:
    return SystolicUnit(name="systolic_unit", sim=sim or Simulation(), **{**CENTRE, **cfg})


@dataclass(kw_only=True)
class UnitCodegenStep(BuildStep):
    """``codegen``: the linalg headers, the unit's free-running top and its csynth script."""

    description = "Headers, the systolic unit's top and its csynth script."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}
    cfg: dict

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False   # Python in, cheap; csynth decides by content

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        mod = unit(**self.cfg)
        parts = collect_parts(mod)
        dag = linalg_headers_dag(parts.traits, parts.bodies, INCLUDE_DIR, [32, 64], parts.schemas)
        dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
        res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"header generation failed: {bad}")
        spec = composite_top_spec(mod, width=int(mod.word_bits))
        gen = root / GEN_DIR
        gen.mkdir(parents=True, exist_ok=True)
        (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
        tcl_path(root, spec.top_name).write_text(
            render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS), encoding="utf-8")
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def steady(u: SystolicUnit, njobs: int = 4, op: int = 1) -> dict:
    """A stimulus of *njobs* identical full-size requests back to back: the steady interval's
    scenario (``workload`` takes it as a one-off)."""
    import numpy as np

    from waveflow.linalg.lanes import to_words
    from waveflow.linalg.message import header
    from waveflow.linalg.systolic import stored_shape
    from waveflow.utils.burst_io import StreamBurst

    m, k, n = int(u.Mmax), int(u.Kmax), int(u.Nmax)
    rng = np.random.default_rng(1)
    bursts = []
    for j in range(njobs):
        a = rng.integers(-2048, 2048, (2, *stored_shape(op, m, k)))
        b = rng.integers(-2048, 2048, (2, k, n))
        aw, bw = to_words(*a, u.a), to_words(*b, u.b)
        h = header(100 + j, op, m=m, k=k, n=n, length=len(aw) + len(bw))
        for words in (h.serialize(word_bw=64), aw, bw):
            bursts.append(StreamBurst(np.asarray(words, np.uint64), True))
    return {"s_in": bursts}


def build_config(root) -> BuildConfig:
    """The DAG's config, naming the installed Vitis release: the calibration record's provenance
    carries it (``tool``), so a record says which tool version measured it."""
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    return BuildConfig(root_dir=Path(root), params={},
                       vitis_version=toolchain.tool_version(exe, "--version") if exe else None)


def build_dag(cfg: dict | None = None, *, workload="test", trace: bool = True,
              store_dir: Path = CALIB_DIR, n_cycles: int = 200_000, calibrate: bool = True) -> BuildDag:
    """The unit's harness on *workload* (a kind, or a one-off stimulus) and, with *calibrate*, its
    calibration on ``workload("calib")``, filed in a
    :class:`~waveflow.calib.record_store.ModuleStore` at *store_dir*."""
    cfg = dict(cfg or {})
    sysm = harness(unit(**cfg), workload=workload, n_cycles=n_cycles)
    dag = BuildDag()
    dag.add(UnitCodegenStep(name="codegen", cfg=cfg))
    add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP, trace=trace)
    if calibrate:
        add_calib_steps(dag, harness(unit(**cfg), workload="test", n_cycles=n_cycles),
                        work_dir=WORK_DIR, top=TOP, store=ModuleStore(store_dir))
    return dag


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="the build root (keep it short: see above)")
    ap.add_argument("--through", default="compare")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    res = build_dag().run(build_config(root), through=a.through)
    ok = True
    for name, r in res.items():
        print(f"{name:24} {'ok' if r.success else 'FAIL'}  {'' if r.success else r.message[:3000]}")
        ok &= r.success
    sys.exit(0 if ok else 1)
