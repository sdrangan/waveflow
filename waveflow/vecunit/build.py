"""build.py — the vector unit's C++: its headers, and a harness through the system flow (csynth, XSI).

:func:`vecunit_headers_dag` puts together what a design with a vector unit includes: the stream
utilities, the message schemas (``VecInstr``, ``VecJob``, ``VecResp``, ``VecStatus``), the array
utilities of the queues' memory elements, the configuration-independent ``vecunit_core.h`` and the
configuration's generated ``wf_vecunit_<id>.h`` (:mod:`waveflow.vecunit.hls`).

:func:`build_dag` runs a unit in its own harness through the system flow, as
``examples/systolic/engine_harness.py`` does for the systolic engine::

    dag = build_dag(unit, workload="test")
    dag.run(build_config(root), through="compare")    # codegen, csynth, the RTL run, the comparison
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import Buildable, BuildConfig, BuildDag, BuildStep
from waveflow.linalg.build import linalg_headers_dag
from waveflow.vecunit.hls import (
    CORE_HEADER,
    DEFINES,
    header_name,
    io_traits,
    render_header,
)
from waveflow.vecunit.isa import SCHEMAS

#: The target: the RFSoC 4x2's part, at 250 MHz (the CG core's and the systolic engine's).
PART, PERIOD_NS = "xczu48dr-ffvg1517-2-e", 4
#: The harness's system top and its work directory.
TOP, WORK_DIR = "vecunit_sys", "xsi_work"


class VecUnitHeaderStep(Buildable):
    """Write each configuration's ``wf_vecunit_<id>.h`` into ``output_dir``."""

    def __init__(self, cfgs, output_dir: str | Path = "include") -> None:
        super().__init__()
        self._cfgs = {header_name(c): c for c in cfgs}
        self._output_dir = Path(output_dir)

    @property
    def build_outputs(self) -> dict[str, Path]:
        return {name: self._output_dir / name for name in self._cfgs}

    def generate(self, key: str, config: BuildConfig) -> str:
        return render_header(self._cfgs[key], self._output_dir.as_posix())


def vecunit_headers_dag(cfgs, include_dir: str = "include", word_bits=None) -> BuildDag:
    """The headers of a design whose vector units have configurations ``cfgs``."""
    cfgs = list(cfgs)
    wb = sorted({64, *(word_bits or ()), *(c.word_bits for c in cfgs)})
    dag = linalg_headers_dag([io_traits(c) for c in cfgs], (CORE_HEADER,), include_dir, wb, SCHEMAS)
    dag.add(VecUnitHeaderStep(cfgs, include_dir))
    return dag


def build_config(root) -> BuildConfig:
    """The DAG's config, naming the installed Vitis release (a measurement names its tool)."""
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    return BuildConfig(root_dir=Path(root), params={},
                       vitis_version=toolchain.tool_version(exe, "--version") if exe else None)


@dataclass(kw_only=True)
class VecUnitCodegenStep(BuildStep):
    """``codegen``: the headers, the unit's free-running top and its csynth script."""

    description = "Headers, the vector unit's top and its csynth script."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path("include"), "gen": Path("gen")}
    make: Any = field(default=None)  # () -> the unit (a fresh VecUnit)

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
        from waveflow.build.streamutils import MemMgrStep

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        unit = self.make()
        dag = vecunit_headers_dag([unit.cfg], INCLUDE_DIR)
        dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
        res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            raise RuntimeError(f"header generation failed: {bad}")
        spec = composite_top_spec(unit, width=int(unit.cfg.word_bits))
        gen = root / GEN_DIR
        gen.mkdir(parents=True, exist_ok=True)
        (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
        tcl_path(root, spec.top_name).write_text(
            render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS, defines=DEFINES),
            encoding="utf-8")
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(unit, *, workload="test", n_cycles: int = 200_000, trace: bool = False) -> BuildDag:
    """A unit (an instance) in its harness on ``workload``, through the system flow."""
    from waveflow.build.system_dag import add_system_steps
    from waveflow.hw.harness import harness

    def make():
        from waveflow.hw.harness import _fresh_module
        from waveflow.simulation.simulation import Simulation

        return _fresh_module(unit, Simulation())

    sysm = harness(unit, workload=workload, n_cycles=n_cycles)
    dag = BuildDag()
    dag.add(VecUnitCodegenStep(name="codegen", make=make))
    add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP, trace=trace)
    return dag


__all__ = ["PART", "PERIOD_NS", "VecUnitCodegenStep", "VecUnitHeaderStep", "build_config",
           "build_dag", "vecunit_headers_dag"]
