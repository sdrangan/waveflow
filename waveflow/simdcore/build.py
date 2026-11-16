"""build.py — the SIMD core through the system flow (``plans/simdcore.md`` Stage 5): its headers, its
generated top, csynth and the RTL run in its harness, compared with pysim burst for burst.

    dag = build_dag(core, workload="test")
    dag.run(build_config(root), through="compare")    # codegen, csynth, the RTL run, the comparison

as :mod:`waveflow.vecunit.build` does for the vector unit.  The top is the composite generator's
(``composite_top_spec``): two ``hls::task``\\ s, the scalar and the vector task
(:class:`~waveflow.simdcore.unit.SimdScalar`, :class:`~waveflow.simdcore.unit.SimdVector`), joined by
``vq`` and ``rq``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.simdcore.csynth import PART, PERIOD_NS
from waveflow.simdcore.hls import CORE_HEADER, header_name, render_header

#: The harness's system top and its work directory.
TOP, WORK_DIR = "simdcore_sys", "xsi_work"
BUILD = Path(__file__).resolve().parents[1] / "build"


def build_config(root) -> BuildConfig:
    """The DAG's config, naming the installed Vitis release (a measurement names its tool)."""
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    return BuildConfig(root_dir=Path(root), params={},
                       vitis_version=toolchain.tool_version(exe, "--version") if exe else None)


@dataclass(kw_only=True)
class SimdCoreCodegenStep(BuildStep):
    """``codegen``: the headers, the core's free-running top and its csynth script."""

    description = "Headers, the SIMD core's top and its csynth script."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path("include"), "gen": Path("gen")}
    make: Any = field(default=None)  # () -> the core (a fresh SimdCore)

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from waveflow.build.composite_gen import (
            GEN_DIR,
            INCLUDE_DIR,
            composite_top_spec,
            render_tcl,
            render_top,
            tcl_path,
        )
        from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        core = self.make()
        inc = root / INCLUDE_DIR
        inc.mkdir(parents=True, exist_ok=True)
        dag = BuildDag()
        dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
        dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
        res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"header generation failed: {bad}")
        shutil.copyfile(BUILD / CORE_HEADER, inc / CORE_HEADER)
        (inc / header_name(core.cfg)).write_text(render_header(core.cfg, top=False), encoding="utf-8")
        spec = composite_top_spec(core, width=64)
        gen = root / GEN_DIR
        gen.mkdir(parents=True, exist_ok=True)
        (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
        tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS),
                                                 encoding="utf-8")
        return {"include": inc, "gen": gen}


def build_dag(core, *, workload="test", n_cycles: int = 400_000, trace: bool = False) -> BuildDag:
    """A core (an instance) in its harness on ``workload``, through the system flow."""
    from waveflow.build.system_dag import add_system_steps
    from waveflow.hw.harness import harness

    def make():
        from waveflow.hw.harness import _fresh_module
        from waveflow.simulation.simulation import Simulation

        return _fresh_module(core, Simulation())

    sysm = harness(core, workload=workload, n_cycles=n_cycles)
    dag = BuildDag()
    dag.add(SimdCoreCodegenStep(name="codegen", make=make))
    add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP, trace=trace)
    return dag


__all__ = ["SimdCoreCodegenStep", "TOP", "WORK_DIR", "build_config", "build_dag"]
