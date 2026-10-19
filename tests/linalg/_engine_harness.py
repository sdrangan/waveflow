"""The systolic engine in its own harness, stream-fed: pysim + golden, csynth, XSI
(``plans/systolic_characterization.md``, Part B).

    python -m tests.linalg._engine_harness --root C:/wfsc/eh --through golden      # pysim only
    python -m tests.linalg._engine_harness --root C:/wfsc/eh --through compare     # + csynth, XSI

Build from a short root (Windows path length: ``plans/systolic_characterization_lessons.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl,
                                          render_top, tcl_path)
from waveflow.build.streamutils import MemMgrStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.harness import harness
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.linalg.systolic_engine import SystolicEngine
from waveflow.simulation.simulation import Simulation

from tests.linalg._hls import PART, PERIOD_NS
from tests.linalg._systolic_harness import build_config, reg

TOP, WORK_DIR = "systolic_engine_sys", "xsi_work"

CENTRE = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4, form=4, word_bits=64,
              a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))


def engine(sim=None, **cfg) -> SystolicEngine:
    return SystolicEngine(name="systolic_engine", sim=sim or Simulation(), **{**CENTRE, **cfg})


@dataclass(kw_only=True)
class EngineCodegenStep(BuildStep):
    """``codegen``: the linalg headers, the engine's free-running top and its csynth script."""

    description = "Headers, the systolic engine's top and its csynth script."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}
    cfg: dict

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        mod = engine(**self.cfg)
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


def build_dag(cfg: dict | None = None, *, workload="test", trace: bool = False,
              n_cycles: int = 200_000) -> BuildDag:
    cfg = dict(cfg or {})
    sysm = harness(engine(**cfg), workload=workload, n_cycles=n_cycles)
    dag = BuildDag()
    dag.add(EngineCodegenStep(name="codegen", cfg=cfg))
    add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP, trace=trace)
    return dag


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--through", default="compare")
    ap.add_argument("--workload", default="test")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    res = build_dag(workload=a.workload, trace=a.trace).run(build_config(root), through=a.through)
    ok = True
    for name, r in res.items():
        print(f"{name:24} {'ok' if r.success else 'FAIL'}  {'' if r.success else r.message[:3000]}")
        ok &= r.success
    sys.exit(0 if ok else 1)
