"""The memory-backed systolic engine in its own harness: jobs on ``s_job``, operands and results in the
harness's one memory, a done per job on ``s_done`` (``plans/systolic_characterization.md``, Part B).

    python -m tests.linalg._engine_mem_harness --root C:/wfsc/em --through golden    # pysim
    python -m tests.linalg._engine_mem_harness --root C:/wfsc/em --through compare   # + csynth, XSI
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl,
                                          render_top, tcl_path)
from waveflow.build.streamutils import MemMgrStep, MemStreamStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.harness import harness
from waveflow.hw.mem_stream import MemRCmd, MemWCmd
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.linalg.systolic_engine_mem import SystolicEngineMem
from waveflow.simulation.simulation import Simulation

from tests.linalg._hls import PART, PERIOD_NS
from tests.linalg._systolic_harness import build_config, reg

TOP, WORK_DIR = "systolic_engine_mem_sys", "xsi_work"

CENTRE = dict(Mmax=8, Kmax=8, Nmax=32, Pmax=4, R=4, C=8, L=4, form=4, word_bits=64,
              a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))


def engine_mem(sim=None, **cfg) -> SystolicEngineMem:
    return SystolicEngineMem(name="systolic_engine_mem", sim=sim or Simulation(), **{**CENTRE, **cfg})


@dataclass(kw_only=True)
class EngineMemCodegenStep(BuildStep):
    description = "Headers, the memory-backed engine's top and its csynth script."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}
    cfg: dict

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        mod = engine_mem(**self.cfg)
        parts = collect_parts(mod)
        words = sorted({32, 64, int(mod.word_bits)})
        schemas = tuple(s for s in parts.schemas if s not in (MemRCmd, MemWCmd))
        dag = linalg_headers_dag(parts.traits, parts.bodies, INCLUDE_DIR, words, schemas)
        dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
        dag.add(MemStreamStep(output_dir=INCLUDE_DIR))
        for cls in (MemRCmd, MemWCmd):
            dag.add(DataSchemaStep(cls, word_bw_supported=words, include_dir=INCLUDE_DIR, framed=True))
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
              n_cycles: int = 400_000) -> BuildDag:
    cfg = dict(cfg or {})
    sysm = harness(engine_mem(**cfg), workload=workload, n_cycles=n_cycles)
    dag = BuildDag()
    dag.add(EngineMemCodegenStep(name="codegen", cfg=cfg))
    add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP, trace=trace)
    return dag


def check_c(mem_words: np.ndarray, kind: str = "test", **cfg) -> list[int]:
    """Words of each job's C in *mem_words* (the arena after the run) that differ from the model."""
    m = engine_mem(**cfg)
    return [int(np.count_nonzero(mem_words[base:base + len(want)] != want))
            for base, want in m.expected_c(kind)]


if __name__ == "__main__":
    import argparse
    import sys

    from waveflow.utils.burst_io import read_burst_bundle

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--through", default="compare")
    ap.add_argument("--workload", default="test")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    res = build_dag(workload=a.workload).run(build_config(root), through=a.through)
    ok = True
    for name, r in res.items():
        print(f"{name:24} {'ok' if r.success else 'FAIL'}  {'' if r.success else r.message[:3000]}")
        ok &= r.success
    work = root / WORK_DIR / TOP
    for label, d in (("pysim", work / "pysim_run" / "vectors" / "mem"),
                     ("rtl", work / "xsi_run" / "vectors" / "mem")):
        if d.is_dir():
            words = np.asarray(read_burst_bundle(d)[0], np.uint64)
            print(f"{label}: C words wrong per job {check_c(words, a.workload)}")
    sys.exit(0 if ok else 1)
