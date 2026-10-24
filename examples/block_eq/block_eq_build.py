"""block_eq_build.py -- the block equalizer's build DAG.

    python -m examples.block_eq.block_eq_build --through pysim        # pysim only
    python -m examples.block_eq.block_eq_build                        # the composite through compare
    python -m examples.block_eq.block_eq_build --through gain_compare  # gain alone, at RTL
    python -m examples.block_eq.block_eq_build --through apply_compare # apply alone, at RTL
    python -m examples.block_eq.block_eq_build --through early_compare # the early-TLAST scenario

The two stages' bodies are hand-written (``src/``); everything else is generated: the schemas' C++
(with framed readers and writers, for the internal FIFOs), the float32 lane routines, the framework's
StreamBroadcast body, and three free-running tops -- the composite, and each stage alone, so each stage
is verified in its own harness at RTL before the composite.  One body serves a stage in both places:
its port types are template parameters the generator supplies (``KernelTask.framed``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl, render_top, tcl_path
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamBroadcastStep

from examples.block_eq.block_eq import SCHEMA_CLASSES, W, Apply, BlockEq, Float32, Gain

HERE = Path(__file__).resolve().parent
WORK_DIR = "xsi_work"
#: The tops: the composite, and each stage alone.
TOP, GAIN_TOP, APPLY_TOP = "block_eq_top", "eq_gain_top", "eq_apply_top"


def gen_headers(root: Path = HERE) -> None:
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=[W], include_dir=INCLUDE_DIR, framed=True))
    dag.add(ArrayUtilsStep(Float32, [W]))
    dag.add(StreamBroadcastStep(output_dir=INCLUDE_DIR))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(cls, root: Path = HERE) -> str:
    mod = cls(name=cls.cpp_kernel_name, sim=Simulation())
    spec = composite_top_spec(mod, width=W)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name), encoding="utf-8")
    return spec.top_name


def generate(root: Path = HERE) -> list[str]:
    """Everything before csynth: the headers, and the three tops with their ``.tcl``."""
    gen_headers(root)
    return [gen_top(c, root) for c in (BlockEq, Gain, Apply)]


@dataclass(kw_only=True)
class BlockEqCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh; csynth decides by content."""

    description = "Generate the headers, the lane routines, the broadcast's body and the three tops."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(work_dir=WORK_DIR) -> BuildDag:
    """``codegen``; the composite's harness (no prefix); each stage's harness (``gain_``, ``apply_``);
    and the composite on the early-TLAST scenario (``early_``)."""
    dag = BuildDag()
    dag.add(BlockEqCodegenStep(name="codegen"))
    add_system_steps(dag, harness(BlockEq, workload="test"), work_dir=work_dir, top=TOP)
    add_system_steps(dag, harness(Gain, workload="test"), work_dir=work_dir, top=GAIN_TOP, prefix="gain_")
    add_system_steps(dag, harness(Apply, workload="test"), work_dir=work_dir, top=APPLY_TOP, prefix="apply_")
    add_system_steps(dag, harness(BlockEq, workload="early"), work_dir=work_dir, top=TOP, prefix="early_")
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import PLATFORM_ARGS, platform_config, run_dag_cli
    from waveflow.build.composite_gen import DEFAULT_PART, DEFAULT_PERIOD_NS

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0], default_through="compare",
                root_dir=HERE,
                extra_args=[(("--synth",), dict(choices=("build", "check"), default="build",
                             help="build: csynth a stale top; check: fail on one instead")),
                            *PLATFORM_ARGS],
                params_from_args=lambda a: {"synth": a.synth},
                config_from_args=lambda a: platform_config(a, part=DEFAULT_PART,
                                                           clk_freq=1e9 / DEFAULT_PERIOD_NS))
